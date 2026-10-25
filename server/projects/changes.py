"""Explicit customer instructions: append revisions, never rewrite past execution."""
import hashlib,json,uuid
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404,render,redirect
from django.utils import timezone
from accounts.models import User
from accounts.permissions import permitted
from accounts.views import audit
from documents.access import visible_documents
from documents.models import Document
from .access import visible_contracts,visible_objects,contract_card
from .models import Contract,ContractChange,ContractChangeRevision,ContractChangeDecision,ContractChangeCommercial,ContractChangeSubmission

IMPACTS=[('volumes','Объёмы'),('materials','Материалы'),('dates','Сроки'),('cost','Стоимость')]
FIELDS=['title','received_on','effective_from','scope','before_text','after_text','basis_kind','basis_note']

def allowed(user):return permitted(user,'projects.manage') or permitted(user,'contract.changes')

def scoped_contract(user,pk,lock=False):
    if not allowed(user):raise PermissionDenied
    qs=Contract.objects.select_for_update(no_key=True) if lock else Contract.objects.all()
    contract=get_object_or_404(qs.filter(pk__in=visible_contracts(user).values('pk')),pk=pk)
    if not user.is_leader and contract.objects_linked.exclude(pk__in=visible_objects(user)).exists():raise PermissionDenied
    return contract

def basis_choices(user):return visible_documents(user).filter(archived=False,current_version__isnull=False).order_by('title')

class ChangeForm(forms.Form):
    token=forms.UUIDField(widget=forms.HiddenInput)
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    title=forms.CharField(label='Что просит изменить заказчик',max_length=200)
    received_on=forms.DateField(label='Когда получено указание',widget=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}))
    effective_from=forms.DateField(label='С какого дня применять',widget=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}))
    objects=forms.ModelMultipleChoiceField(label='Объекты',queryset=Document.objects.none(),widget=forms.CheckboxSelectMultiple(attrs={'data-bulk-check':'change-objects'}))
    scope=forms.CharField(label='Работы, секции и границы применения',max_length=3000,widget=forms.Textarea)
    before_text=forms.CharField(label='Как было',max_length=3000,required=False,widget=forms.Textarea)
    after_text=forms.CharField(label='Как должно быть',max_length=3000,widget=forms.Textarea)
    basis_kind=forms.ChoiceField(label='Как получено указание',choices=ContractChangeRevision._meta.get_field('basis_kind').choices)
    basis_note=forms.CharField(label='Кто дал указание и основание (номер письма, сообщение, запись об устном указании)',max_length=3000,widget=forms.Textarea)
    basis_document=forms.ModelChoiceField(label='Документ-основание (необязательно)',queryset=Document.objects.none(),required=False)
    impacts=forms.MultipleChoiceField(label='На что влияет',choices=IMPACTS,widget=forms.CheckboxSelectMultiple,required=False)
    def __init__(self,*args,user,contract,change=None,**kwargs):
        initial={'token':uuid.uuid4(),'expected_version':change.version if change else 0,'received_on':timezone.localdate(),'effective_from':timezone.localdate()}
        if change:
            rev=change.current_revision
            initial.update({f:getattr(rev,f) for f in FIELDS});initial.update(objects=rev.object_ids,impacts=rev.impacts)
            if rev.basis_version_id:initial['basis_document']=rev.basis_version.document_id
        kwargs['initial']=initial
        super().__init__(*args,**kwargs)
        self.fields['objects'].queryset=visible_objects(user).filter(contracts=contract)
        self.fields['basis_document'].queryset=basis_choices(user)
    def clean(self):
        data=super().clean()
        if data.get('basis_kind')=='document' and not data.get('basis_document'):self.add_error('basis_document','Выберите доступный документ-основание.')
        return data

@transaction.atomic
def submit(user,contract_id,change_id,data):
    actor=User.objects.select_for_update(no_key=True).get(pk=user.pk)
    if not actor.is_active or actor.session_version!=user.session_version:raise PermissionDenied
    contract=scoped_contract(actor,contract_id,True)
    if contract.status=='archived':raise PermissionDenied('Контракт в архиве — только просмотр.')
    values={f:str(data[f]) for f in FIELDS}
    object_ids=sorted(str(o.pk) for o in data['objects'])
    if not object_ids or set(object_ids)-set(str(pk) for pk in visible_objects(actor).filter(contracts=contract).values_list('pk',flat=True)):
        raise ValidationError('Объекты или доступ изменились. Обновите форму.')
    document=data.get('basis_document');basis=None
    if document:
        doc=get_object_or_404(basis_choices(actor).select_for_update(no_key=True),pk=document.pk)
        basis=doc.current_version
    canonical={**values,'objects':object_ids,'impacts':sorted(data['impacts']),'basis':str(basis.pk) if basis else '', 'change':str(change_id or ''),'version':data['expected_version'],'contract':str(contract.pk)}
    digest=hashlib.sha256(json.dumps(canonical,sort_keys=True).encode()).hexdigest()
    receipt=ContractChangeSubmission.objects.filter(token=data['token']).first()
    if receipt:
        if receipt.actor_id!=actor.pk or receipt.digest!=digest:raise ValidationError('Этот номер отправки уже использован. Обновите форму.')
        return receipt.change
    change=get_object_or_404(ContractChange.objects.select_for_update(no_key=True),pk=change_id,contract=contract) if change_id else None
    if change and change.version!=data['expected_version']:raise ValidationError('Изменение уже обновлено. Откройте актуальную запись.')
    if not change and data['expected_version']!=0:raise ValidationError('Обновите форму новой записи.')
    if change and change.state=='review':raise ValidationError('Запись уже на рассмотрении. Сначала руководитель возвращает её на доработку.')
    if not change:change=ContractChange.objects.create(contract=contract,created_by=actor)
    else:change.version+=1
    revision=ContractChangeRevision.objects.create(change=change,number=change.revisions.count()+1,object_ids=object_ids,impacts=data['impacts'],basis_version=basis,author=actor,**{f:data[f] for f in FIELDS})
    change.current_revision=revision;change.state='review';change.save(update_fields=['current_revision','state','version'])
    ContractChangeSubmission.objects.create(token=data['token'],actor=actor,change=change,revision=revision,digest=digest)
    audit(actor,'contract_updated',entity_id=str(contract.pk),kind='customer_change_submitted',change=str(change.pk),revision=revision.number)
    return change

class DecisionForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    action=forms.ChoiceField(label='Решение',choices=[('approved','Подтвердить указание'),('revision','Вернуть на доработку')])
    note=forms.CharField(label='Пояснение решения',max_length=3000,widget=forms.Textarea)

class CommercialForm(forms.Form):
    revision=forms.ModelChoiceField(label='Подтверждённая редакция указания',queryset=ContractChangeRevision.objects.none())
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    status=forms.ChoiceField(label='Оформление изменения договора',choices=ContractChangeCommercial._meta.get_field('status').choices)
    note=forms.CharField(label='Основание / пояснение — видят только руководители',max_length=3000,widget=forms.Textarea)
    document=forms.ModelChoiceField(label='Оформленный документ',queryset=Document.objects.none(),required=False)
    def __init__(self,*args,user,change,**kwargs):
        super().__init__(*args,**kwargs);self.fields['document'].queryset=basis_choices(user)
        self.fields['revision'].queryset=change.revisions.filter(decision__action='approved')
        self.fields['revision'].label_from_instance=lambda r:f'Редакция {r.number} · {r.title}'
        self.initial.setdefault('revision',change.active_revision_id)
    def clean(self):
        data=super().clean()
        if data.get('status')=='signed' and not data.get('document'):self.add_error('document','Для статуса «оформлено» выберите документ.')
        return data

@transaction.atomic
def decide(user,pk,version,action,note,document=None,revision=None):
    actor=User.objects.select_for_update(no_key=True).get(pk=user.pk)
    if not actor.is_active or actor.session_version!=user.session_version or not actor.is_leader:raise PermissionDenied
    stub=get_object_or_404(ContractChange,pk=pk)
    contract=scoped_contract(actor,stub.contract_id,True)
    if contract.status=='archived':raise PermissionDenied('Контракт в архиве — только просмотр.')
    change=ContractChange.objects.select_for_update(no_key=True).get(pk=pk)
    if change.version!=version:raise ValidationError('Запись уже изменена. Обновите страницу.')
    if not note.strip() or len(note)>3000:raise ValidationError('Укажите пояснение до 3000 символов.')
    if action in ('approved','revision'):
        if change.state!='review':raise ValidationError('Эта редакция уже рассмотрена.')
        ContractChangeDecision.objects.create(revision=change.current_revision,actor=actor,action=action,note=note.strip())
        change.state=action
        if action=='approved':change.active_revision=change.current_revision
    else:
        if action not in dict(ContractChangeCommercial._meta.get_field('status').choices):raise ValidationError('Неизвестный статус оформления.')
        target=revision or change.active_revision
        if not target or not change.revisions.filter(pk=target.pk,decision__action='approved').exists():raise ValidationError('Выберите подтверждённую редакцию этого указания.')
        basis=None
        if document:basis=get_object_or_404(basis_choices(actor).select_for_update(no_key=True),pk=document.pk).current_version
        if action=='signed' and not basis:raise ValidationError('Выберите оформленный документ.')
        ContractChangeCommercial.objects.create(revision=target,actor=actor,status=action,note=note.strip(),document_version=basis)
    change.version+=1;change.save(update_fields=['state','active_revision','version'])
    audit(actor,'contract_updated',entity_id=str(contract.pk),kind='customer_change_'+action,change=str(change.pk),revision=change.current_revision.number)
    return change

@login_required
def index(request,contract_id):
    contract=scoped_contract(request.user,contract_id)
    return render(request,'projects/change_list.html',{'card':contract_card(contract,request.user),'rows':contract.customer_changes.select_related('current_revision','active_revision').order_by('-created_at')})

@login_required
def edit(request,contract_id,pk=None):
    contract=scoped_contract(request.user,contract_id)
    if contract.status=='archived':raise PermissionDenied
    change=get_object_or_404(contract.customer_changes,pk=pk) if pk else None
    if change and change.state=='review' and request.method!='POST':raise PermissionDenied('Редакция на рассмотрении.')
    form=ChangeForm(request.POST or None,user=request.user,contract=contract,change=change)
    if request.method=='POST' and form.is_valid():
        try:saved=submit(request.user,contract.pk,change.pk if change else None,form.cleaned_data)
        except ValidationError as exc:form.add_error(None,exc)
        else:
            messages.success(request,'Указание отправлено руководителю. Предыдущие записи и выполнения не изменены.')
            return redirect('contract_change',pk=saved.pk)
    return render(request,'projects/change_form.html',{'card':contract_card(contract,request.user),'form':form})

@login_required
def detail(request,pk):
    change=get_object_or_404(ContractChange.objects.select_related('contract','current_revision','active_revision'),pk=pk)
    contract=scoped_contract(request.user,change.contract_id)
    decision=DecisionForm(initial={'expected_version':change.version});commercial=CommercialForm(user=request.user,change=change,initial={'expected_version':change.version})
    if request.method=='POST':
        if not request.user.is_leader or contract.status=='archived':raise PermissionDenied
        commercial_post=request.POST.get('kind')=='commercial'
        form=CommercialForm(request.POST,user=request.user,change=change) if commercial_post else DecisionForm(request.POST)
        if commercial_post:commercial=form
        else:decision=form
        if form.is_valid():
            try:decide(request.user,change.pk,form.cleaned_data['expected_version'],form.cleaned_data['status' if commercial_post else 'action'],form.cleaned_data['note'],form.cleaned_data.get('document'),form.cleaned_data.get('revision'))
            except ValidationError as exc:form.add_error(None,exc)
            else:return redirect('contract_change',pk=pk)
    docs=set(visible_documents(request.user).values_list('pk',flat=True));objects={str(o.pk):o.name for o in visible_objects(request.user)}
    history=[]
    for rev in change.revisions.select_related('author','basis_version__document').prefetch_related('commercial_events__document_version__document','commercial_events__actor'):
        commercial_rows=[{'event':event,'file':event.document_version if event.document_version and event.document_version.document_id in docs else None} for event in rev.commercial_events.all()]
        history.append({'revision':rev,'objects':[objects.get(str(i),'Объект недоступен') for i in rev.object_ids],'basis':rev.basis_version if rev.basis_version and rev.basis_version.document_id in docs else None,'decision':ContractChangeDecision.objects.filter(revision=rev).select_related('actor').first(),'commercial':commercial_rows})
    return render(request,'projects/change_detail.html',{'card':contract_card(contract,request.user),'change':change,'history':history,'decision_form':decision,'commercial_form':commercial})

@login_required
def queue(request):
    if not request.user.is_leader:raise PermissionDenied
    rows=ContractChange.objects.filter(contract__in=visible_contracts(request.user),state='review').exclude(contract__status='archived').select_related('contract','current_revision').order_by('created_at')
    return render(request,'projects/change_queue.html',{'rows':[{'change':row,'card':contract_card(row.contract,request.user)} for row in rows]})
