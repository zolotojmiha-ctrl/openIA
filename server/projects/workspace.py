"""Contract-first workspace and explicit, audited allocation of legacy repairs."""
import uuid
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction,IntegrityError
from django.db.models import Q,Count
from django.shortcuts import get_object_or_404,render,redirect
from accounts.models import User
from accounts.permissions import permitted
from accounts.views import audit
from .models import Contract,ProjectObject,WORK_KINDS
from .access import visible_contracts,visible_objects,contract_card
from repairs.access import visible_jobs,repair_contracts,validate_contract_place,can_prepare
from repairs.models import Job,Defect,Section

@login_required
def home(request):
    if request.user.role=='subcontractor':return redirect('subcontracts')
    q=request.GET.get('q','').strip()[:200]
    contracts=visible_contracts(request.user).order_by('pk')
    archived=request.GET.get('archive')=='1'
    contracts=contracts.filter(status='archived') if archived else contracts.exclude(status='archived')
    if request.GET.get('object'):
        try:contracts=contracts.filter(objects_linked=uuid.UUID(request.GET['object']))
        except ValueError:contracts=contracts.none()
    rows=[]
    for contract in contracts:
        card=contract_card(contract,request.user)
        if q and q.casefold() not in (' '.join(str(v) for v in [card['title'],card['subject']])).casefold():continue
        places=visible_objects(request.user).filter(contracts=contract)
        rows.append({'card':card,'kind':contract.work_kinds_display,'places':places,'status':contract.get_status_display() if request.user.is_leader else '—'})
    legacy=visible_jobs(request.user).filter(contract__isnull=True).count()
    return render(request,'projects/work_home.html',{'archived_contracts':archived,'rows':rows,'q':q,'legacy_count':legacy,'can_allocate':can_prepare(request.user) and permitted(request.user,'projects.manage')})

@login_required
def contract(request,pk):
    item=get_object_or_404(visible_contracts(request.user),pk=pk)
    places=visible_objects(request.user).filter(contracts=item).order_by('name')
    qs=visible_jobs(request.user).filter(contract=item)
    from .daily import works as daily_works
    daily_rows=daily_works(request.user).filter(contract=item)
    return render(request,'projects/work_contract.html',{'card':contract_card(item,request.user),'kinds':item.work_kinds_display,'places':places,'job_count':qs.count(),'daily_count':daily_rows.count(),'review_count':qs.filter(state='reported').count()+daily_rows.filter(state='reported').count(),'work_contract_id':item.pk,'work_types':[{'code':k,'name':dict(WORK_KINDS)[k]} for k in item.work_kinds if k in dict(WORK_KINDS)]})

@login_required
def place(request,pk,object_id):
    item=get_object_or_404(visible_contracts(request.user),pk=pk)
    obj=get_object_or_404(visible_objects(request.user).filter(contracts=item),pk=object_id)
    sections=Section.objects.filter(project_object=obj).order_by('pipeline','name')
    rows=[]
    for section in sections:
        qs=visible_jobs(request.user).filter(contract=item,section=section)
        rows.append({'section':section,'total':qs.count(),'review':qs.filter(state='reported').count()})
    return render(request,'projects/work_place.html',{'card':contract_card(item,request.user),'object':obj,'rows':rows,'has_repairs':'repairs' in item.work_kinds,'other_types':[{'code':k,'label':dict(WORK_KINDS)[k]} for k in item.work_kinds if k in dict(WORK_KINDS) and k!='repairs']})

class AllocationForm(forms.Form):
    snapshot=forms.CharField(widget=forms.HiddenInput)
    contract=forms.ModelChoiceField(label='Контракт, к которому относятся выбранные работы',queryset=Contract.objects.none())
    jobs=forms.ModelMultipleChoiceField(label='Старые работы без контракта',queryset=Job.objects.none(),widget=forms.CheckboxSelectMultiple(attrs={'data-bulk-check':'jobs'}))
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['contract'].queryset=repair_contracts(user)
        self.has_contracts=self.fields['contract'].queryset.exists()
        if not self.has_contracts:self.fields['contract'].empty_label='Нет контрактов с видом работ «Устранение дефектов»'
        self.fields['contract'].label_from_instance=lambda c:contract_card(c,user)['title']
        qs=visible_jobs(user,include_archived=True).filter(contract__isnull=True).order_by('section__name','pk')
        ids=list(qs.values_list('pk',flat=True)[:100])
        qs=qs.filter(pk__in=ids)
        self.fields['jobs'].queryset=qs
        self.fields['jobs'].label_from_instance=lambda j:f'{j.section} · '+', '.join(str(d) for d in j.defects.all())+' · '+j.defect_status
        self.initial['snapshot']=signing.dumps({str(j.pk):j.version for j in qs},salt='contract-allocation')

def allocate_legacy(user,contract,ids,snapshot):
    """No automatic inference; preserve PKs, reports, acceptance and original imports."""
    with transaction.atomic():
        actor=User.objects.select_for_update().get(pk=user.pk)
        if not can_prepare(actor) or not permitted(actor,'projects.manage'):raise PermissionDenied
        target=get_object_or_404(Contract.objects.select_for_update().filter(pk__in=repair_contracts(actor).values('pk')),pk=contract.pk)
        section_ids=visible_jobs(actor,include_archived=True).filter(pk__in=ids).values('section_id')
        list(Section.objects.select_for_update().filter(pk__in=section_ids).order_by('pk'))
        jobs=list(visible_jobs(actor,include_archived=True).select_for_update(no_key=True,of=('self',)).filter(pk__in=ids).order_by('pk'))
        if len(jobs)!=len(ids):raise ValidationError('Доступ или состав работ изменился.')
        for job in jobs:
            if job.contract_id or snapshot.get(str(job.pk))!=job.version:raise ValidationError('Работа изменилась. Обновите список.')
            validate_contract_place(target,job.section)
        # Same section lock order as manual creation/import prevents identity races.
        list(Section.objects.select_for_update().filter(pk__in={j.section_id for j in jobs}).order_by('pk'))
        defect_ids=Defect.objects.filter(jobs__pk__in=ids).values('pk')
        defects=list(Defect.objects.select_for_update().filter(pk__in=defect_ids).order_by('pk'))
        for defect in defects:
            if defect.jobs.exclude(pk__in=ids).exists():raise ValidationError('Выберите вместе все работы, связанные с одним дефектом. Разделять их историю автоматически нельзя.')
            from repairs.values import matching_defects
            if matching_defects(defect.section,defect.kilometre,defect.number,target).exclude(pk=defect.pk).exists():raise ValidationError('В контракте уже есть такой дефект. Нужно проверить совпадение, история не объединяется автоматически.')
        for defect in defects:
            defect.contract=target;defect.save(update_fields=['contract'])
        for job in jobs:
            job.contract=target;job.version+=1;job.save(update_fields=['contract','version'])
            audit(actor,'repair_prepared',entity_id=str(job.pk),kind='contract_allocation',contract=str(target.pk),previous_contract=None)

@login_required
def legacy(request):
    if not can_prepare(request.user) or not permitted(request.user,'projects.manage'):raise PermissionDenied
    form=AllocationForm(request.POST or None,user=request.user)
    if request.method=='POST' and form.is_valid():
        try:
            snapshot=signing.loads(form.cleaned_data['snapshot'],salt='contract-allocation',max_age=3600)
            allocate_legacy(request.user,form.cleaned_data['contract'],[j.pk for j in form.cleaned_data['jobs']],snapshot)
        except (signing.BadSignature,ValidationError,IntegrityError) as e:
            form.add_error(None,e.messages[0] if isinstance(e,ValidationError) else 'Список устарел или найдено совпадение. Обновите страницу и проверьте выбор.')
        else:
            messages.success(request,'Работы связаны с контрактом. Исходные записи, результаты, файлы и права на документы сохранены.')
            return redirect('work_contract',pk=form.cleaned_data['contract'].pk)
    unconfigured=[]
    for contract in visible_contracts(request.user).exclude(status='archived'):
        if not contract.work_kinds and not contract.objects_linked.exclude(pk__in=visible_objects(request.user)).exists():
            unconfigured.append(contract_card(contract,request.user))
    return render(request,'projects/allocate.html',{'form':form,'unconfigured_contracts':unconfigured})

@login_required
def sections_options(request,pk):
    item=get_object_or_404(repair_contracts(request.user),pk=pk)
    sections=Section.objects.filter(project_object__in=visible_objects(request.user).filter(contracts=item)).order_by('pipeline','name')
    from django.http import JsonResponse
    return JsonResponse({'sections':[{'id':str(s.pk),'name':str(s)} for s in sections]})
