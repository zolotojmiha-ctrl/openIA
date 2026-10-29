import uuid
from django import forms
from django.core import signing
from django.core.exceptions import ValidationError,PermissionDenied
from django.shortcuts import render,redirect,get_object_or_404
from django.contrib import messages
from django.views.decorators.http import require_POST
from accounts.views import leader_required
from .filters import filtered_jobs
from .access import visible_jobs
from .review_services import confirm_many,return_for_revision,latest_result,correct_and_accept
from .forms import ReportForm
from .navigation import return_url
from django.urls import reverse

@leader_required
@require_POST
def bulk_review(request):
    error='';jobs=[];snapshot=''
    try:
        if request.POST.get('apply')=='1':
            payload=signing.loads(request.POST.get('snapshot',''),salt='repair-review-selection',max_age=3600)
            if payload['actor']!=str(request.user.pk):raise PermissionDenied
            count=confirm_many(request.user,payload['versions'])
            messages.success(request,f'Подтверждено ремонтов: {count}. Проверка сохранена в истории.')
            return redirect(payload.get('back') or reverse('repairs')+'?state=reported')
        payload=signing.loads(request.POST.get('filter_token',''),salt='repair-review-filter',max_age=3600)
        if payload['actor']!=str(request.user.pk) or payload['filters'].get('archive')=='1':raise PermissionDenied
        qs=filtered_jobs(request.user,payload['filters']).filter(state='reported')
        if request.POST.get('scope')!='all':
            ids={uuid.UUID(v) for v in request.POST.getlist('jobs')}
            qs=qs.filter(pk__in=ids)
            if qs.count()!=len(ids):raise ValidationError('В выборе есть недоступные работы или результаты не на проверке.')
        jobs=list(qs.order_by('pk')[:10001])
        if not jobs:raise ValidationError('Выберите результаты на проверке.')
        if len(jobs)>10000:raise ValidationError('За одно действие можно подтвердить до 10000 ремонтов. Уточните фильтр.')
        snapshot=signing.dumps({'actor':str(request.user.pk),'versions':{str(j.pk):j.version for j in jobs},'back':return_url(request,reverse('repairs')+'?state=reported')},salt='repair-review-selection',compress=True)
    except (signing.BadSignature,ValidationError,ValueError,KeyError,TypeError) as e:
        error=e.messages[0] if isinstance(e,ValidationError) else 'Выбор устарел или некорректен. Вернитесь к списку.'
    return render(request,'repairs/bulk_review.html',{'jobs':jobs,'snapshot':snapshot,'error':error,'defect_count':sum(len(j.active_defects) for j in jobs)},status=409 if error else 200)

class ReturnForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    note=forms.CharField(label='Что нужно исправить',max_length=3000,widget=forms.Textarea(attrs={'rows':4}))

@leader_required
def return_result(request,pk):
    job=get_object_or_404(visible_jobs(request.user),pk=pk)
    form=ReturnForm(request.POST or None,initial={'expected_version':job.version})
    status=200
    if request.method=='POST' and form.is_valid():
        try:return_for_revision(request.user,pk,form.cleaned_data['expected_version'],form.cleaned_data['note'])
        except ValidationError as e:form.add_error(None,e);status=409
        else:messages.success(request,'Результат возвращён мастеру с пояснением. Исходные сведения сохранены.');return redirect('repair_detail',pk=pk)
    return render(request,'repairs/return_result.html',{'job':job,'form':form},status=status)

class ReviewCorrectionForm(ReportForm):
    def __init__(self,*args,job,user,**kwargs):
        super().__init__(*args,job=job,action='correction',user=user,**kwargs)
        result=latest_result(job)
        self.fields['coordinates']=forms.CharField(label='Координаты точки ремонта',max_length=100,help_text='Широта и долгота одной строкой, например: 55.123456, 73.123456.')
        self.fields['outcome'].widget=forms.HiddenInput()
        self.initial['outcome']='complete'
        self.fields['note'].label='Причина исправления'
        if result:
            self.initial.update(performed_date=result.performed_date,method=result.method,length_m=result.length_m,composition=result.composition,coordinates=f'{result.latitude}, {result.longitude}' if result.latitude is not None and result.longitude is not None else '')
    def clean(self):
        c=super().clean()
        if c.get('coordinates'):c['latitude'],c['longitude']=c['coordinates']
        if c.get('outcome')!='complete':self.add_error('outcome','Нужен завершённый результат.')
        return c

@leader_required
def correct_accept(request,pk):
    job=get_object_or_404(visible_jobs(request.user),pk=pk)
    form=ReviewCorrectionForm(request.POST or None,request.FILES or None,job=job,user=request.user)
    status=200
    if request.method=='POST' and form.is_valid():
        try:correct_and_accept(request.user,pk,form.cleaned_data)
        except ValidationError as e:form.add_error(None,e);status=409
        else:messages.success(request,'Правка и подтверждение сохранены. Исходный результат остался в истории.');return redirect('repair_detail',pk=pk)
    return render(request,'repairs/correct_accept.html',{'job':job,'form':form,'original':latest_result(job)},status=status)


class ProblemDecisionForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    decision=forms.ChoiceField(label='Решение руководителя',choices=[('acknowledge','Принять сообщение — ремонт остаётся в работе'),('unrepairable','Дефект не подлежит устранению')],widget=forms.RadioSelect)
    note=forms.CharField(label='Основание решения',max_length=3000,widget=forms.Textarea(attrs={'rows':3}))

@leader_required
def review_problem(request,pk):
    job=get_object_or_404(visible_jobs(request.user),pk=pk)
    initial={'expected_version':job.version,'note':job.problem_note,'decision':'unrepairable' if request.GET.get('decision')=='unrepairable' else 'acknowledge'}
    if job.state=='unrepairable':initial.update(decision='reopen',note='')
    form=ProblemDecisionForm(request.POST or None,initial=initial);status=200
    if job.state=='unrepairable':form.fields['decision'].choices=[('reopen','Вернуть дефект в работу')]
    back=return_url(request,reverse('repair_detail',args=[pk]))
    if request.method=='POST' and form.is_valid():
        from .review_services import decide_problem
        try:decide_problem(request.user,pk,form.cleaned_data['expected_version'],form.cleaned_data['decision'],form.cleaned_data['note'])
        except ValidationError as exc:form.add_error(None,exc);status=409
        else:messages.success(request,'Решение по препятствию сохранено. Исходное сообщение осталось в истории.');return redirect(back)
    return render(request,'repairs/problem_review.html',{'job':job,'form':form,'back':back},status=status)
