from datetime import date
from django import forms
from .models import METHODS
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied,ValidationError
from django.shortcuts import render,redirect,get_object_or_404
from django.views.decorators.http import require_POST
from projects.models import Contract
from projects.access import visible_contracts
from . import archive_import as service

class Upload(forms.Form):
    file=forms.FileField(label='Подготовленный план архива JSON')

class Confirm(forms.Form):
    token=forms.CharField(widget=forms.HiddenInput)
    confirm=forms.BooleanField(label='Подтверждаю перенос только в выбранный архивный договор')

def ticket(user,batch):
    return signing.dumps({'actor':str(user.pk),'session':user.session_version,'batch':str(batch.pk),'digest':batch.payload['digest']},salt='historical-archive')

@login_required
def upload(request,contract_id):
    if not service.allowed(request.user):raise PermissionDenied
    contract=get_object_or_404(visible_contracts(request.user),pk=contract_id)
    form=Upload(request.POST or None,request.FILES or None)
    if request.method=='POST' and form.is_valid():
        try:batch=service.prepare(request.user,contract.pk,form.cleaned_data['file'])
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('repair_archive_review',pk=batch.pk)
    return render(request,'repairs/archive_import.html',{'form':form,'contract':contract})

@login_required
def review(request,pk):
    batch=service.batch_for(request.user,pk)
    initial={'token':ticket(request.user,batch)};form=Confirm(request.POST or None,initial=initial)
    if request.method=='POST' and form.is_valid():
        try:
            data=signing.loads(form.cleaned_data['token'],salt='historical-archive',max_age=86400)
            if (data['actor']!=str(request.user.pk) or data['session']!=request.user.session_version
                or data['batch']!=str(batch.pk) or data['digest']!=batch.payload['digest']):raise signing.BadSignature
            service.apply(request.user,batch.pk,data['digest'])
        except (signing.BadSignature,KeyError):form.add_error(None,'Подтверждение устарело. Обновите предпросмотр.')
        except ValidationError as e:form.add_error(None,e)
        else:
            messages.success(request,'Исторический реестр перенесён в архив. Действующие работы сохранены. Повтор не добавляет дублей.')
            return redirect('work_contract',pk=batch.contract_id)
    rows=[dict(r,display_method='Не уточнён' if r['source'].get('actual_method_unknown') else dict(METHODS)[r['fields']['method']],display_date=date.fromisoformat(r['date']) if r['date'] else None) for r in batch.payload['jobs']]
    return render(request,'repairs/archive_review.html',{'batch':batch,'form':form,'totals':service.counts(batch.payload),'rows':rows})

@login_required
@require_POST
def cancel(request,pk):
    batch=service.batch_for(request.user,pk);cancelled=service.cancel(request.user,pk)
    messages.info(request,'Перенос отменён. Записи не загружены.' if cancelled else 'Перенос уже выполнен. Архив сохранён.')
    return redirect('work_contract',pk=batch.contract_id)
