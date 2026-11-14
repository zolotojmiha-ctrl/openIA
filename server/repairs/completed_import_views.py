from datetime import date
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.shortcuts import render,redirect
from django.views.decorators.http import require_POST
from . import completed_import as service
from .models import METHODS


class Upload(forms.Form):
    file=forms.FileField(label='Подготовленный план выполненных дефектов 2026 (JSON)')


class Confirm(forms.Form):
    token=forms.CharField(widget=forms.HiddenInput)
    confirm=forms.BooleanField(label='Добавить только отсутствующие устранённые дефекты в этот действующий контракт')


@login_required
def upload(request,contract_id):
    contract=service.target(request.user,contract_id)
    form=Upload(request.POST or None,request.FILES or None)
    if request.method=='POST' and form.is_valid():
        try:batch=service.prepare(request.user,contract.pk,form.cleaned_data['file'])
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('repair_completed_review',pk=batch.pk)
    return render(request,'repairs/completed_import.html',{'contract':contract,'form':form})


@login_required
def review(request,pk):
    batch=service.batch_for(request.user,pk)
    token=signing.dumps({'actor':str(request.user.pk),'session':request.user.session_version,'batch':str(pk),'digest':batch.payload['digest']},salt='completed-register')
    form=Confirm(request.POST or None,initial={'token':token})
    if request.method=='POST' and form.is_valid():
        try:
            signed=signing.loads(form.cleaned_data['token'],salt='completed-register',max_age=86400)
            if signed!={'actor':str(request.user.pk),'session':request.user.session_version,'batch':str(pk),'digest':batch.payload['digest']}:raise signing.BadSignature
            service.apply(request.user,batch.pk,signed['digest'])
        except signing.BadSignature:form.add_error(None,'Подтверждение устарело. Обновите предпросмотр.')
        except ValidationError as e:form.add_error(None,e)
        else:
            messages.success(request,'Выполненные дефекты 2026 добавлены. Существующие записи сохранены. '+service.summary(batch))
            return redirect('work_contract',pk=batch.contract_id)
    statuses={'create':'Добавить как устранённый','skip':'Уже устранён — пропустить','hold':'Совпадение — не менять, нужна проверка'}
    rows=[dict(row,action=statuses[state['status']],method=dict(METHODS)[row['fields']['method']],
               day=date.fromisoformat(row['date']) if row['date'] else None) for row,state in zip(batch.payload['rows'],batch.payload['classification'])]
    return render(request,'repairs/completed_review.html',{'batch':batch,'form':form,'summary':service.summary(batch),'rows':rows})


@login_required
@require_POST
def cancel(request,pk):
    batch=service.batch_for(request.user,pk);done=service.cancel(request.user,pk)
    messages.info(request,'Перенос отменён.' if done else 'Этот перенос уже выполнен.')
    return redirect('work_contract',pk=batch.contract_id)
