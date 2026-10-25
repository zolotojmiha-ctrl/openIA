"""Versioned, explicit contract closure decisions; never delete related records."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from accounts.models import User
from accounts.permissions import permitted
from .access import visible_contracts, contract_card
from .models import Contract, ContractClosureEvent
from .archive import lifecycle_write
from .access import visible_objects
from django.db.models import Q
from django.utils import timezone

class ClosureForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    action=forms.ChoiceField(label='Действие',choices=[])
    note=forms.CharField(label='Пояснение',max_length=5000,widget=forms.Textarea)
    def __init__(self,*args,choices,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['action'].choices=choices

def choices(user,contract):
    if not user.is_leader and contract.objects_linked.exclude(pk__in=visible_objects(user)).exists():return []
    latest=contract.closure_events.first()
    if contract.status=='archived':return [('restore','Восстановить из архива')] if user.is_leader else []
    if latest and latest.action=='request':
        return [('archive','Подтвердить закрытие и отправить в архив'),('return','Вернуть на доработку')] if user.is_leader else []
    return [('request','Подать заявку на закрытие')] if permitted(user,'projects.manage') else []

@transaction.atomic
def transition(actor,pk,version,action,note):
    session_version=actor.session_version
    actor=User.objects.select_for_update(no_key=True).get(pk=actor.pk)
    if not actor.is_active or actor.session_version!=session_version:raise PermissionDenied
    contract=get_object_or_404(Contract.objects.select_for_update().filter(pk__in=visible_contracts(actor).values('pk')),pk=pk)
    if action not in dict(choices(actor,contract)):raise PermissionDenied
    if contract.version!=version:raise ValidationError('Контракт или заявка уже изменились. Обновите страницу.')
    if not note.strip():raise ValidationError('Укажите пояснение.')
    old=contract.status
    if action=='archive':
        blockers=closing_checks(contract)
        if blockers:raise ValidationError(['Закрытие невозможно: '+label+' — '+str(count)+'.' for label,count in blockers])
        contract.status='archived'
    elif action=='restore':
        archived=contract.closure_events.filter(action='archive').first()
        contract.status=archived.previous_status if archived and archived.previous_status!='archived' else 'active'
    contract.version+=1
    with lifecycle_write():contract.save(update_fields=['status','version','updated_at'])
    ContractClosureEvent.objects.create(contract=contract,actor=actor,action=action,note=note.strip(),previous_status=old)
    from accounts.views import audit
    audit(actor,'contract_updated',entity_id=str(contract.pk),kind='closure_'+action,reason=note.strip(),previous_status=old,status=contract.status)
    return contract

@login_required
def closure(request,pk):
    contract=get_object_or_404(visible_contracts(request.user),pk=pk)
    available=choices(request.user,contract)
    if not available:raise PermissionDenied
    form=ClosureForm(request.POST or None,choices=available,initial={'expected_version':contract.version})
    if request.method=='POST' and form.is_valid():
        try:transition(request.user,pk,form.cleaned_data['expected_version'],form.cleaned_data['action'],form.cleaned_data['note'])
        except ValidationError as error:form.add_error(None,error)
        else:
            messages.success(request,'Решение сохранено в истории контракта.')
            return redirect('contract_detail',pk=pk)
    return render(request,'projects/closure.html',{'form':form,'card':contract_card(contract,request.user),'blockers':closing_checks(contract),'archived':contract.status=='archived'})


def closing_checks(contract):
    """Operational prerequisites only; finance/doc completion remains an explicit review."""
    from repairs.models import Job,Defect
    from .models import BrigadeAssignment,EquipmentAssignment,SubcontractOrder
    today=timezone.localdate()
    jobs=Job.objects.filter(contract=contract,defects__archived_at__isnull=True).distinct()
    orders=SubcontractOrder.objects.filter(contract=contract,archived=False)
    unfinished_orders=0
    for order in orders:
        if order.kind=='repairs':
            linked=order.repair_jobs.filter(defects__archived_at__isnull=True)
            if not linked.exists() or linked.exclude(state='confirmed').exists():unfinished_orders+=1
        elif order.state!='confirmed':unfinished_orders+=1
    checks=[
        ('Указания заказчика на рассмотрении',contract.customer_changes.filter(state='review').count()),
        ('Ремонты ожидают проверки',jobs.filter(state='reported').count()),
        ('Ремонты не завершены',jobs.exclude(state__in=['reported','confirmed']).count()),
        ('Дефекты без ремонтного задания',Defect.objects.filter(contract=contract,archived_at__isnull=True,jobs__isnull=True).count()),
        ('Незавершённые поручения субподрядчикам',unfinished_orders),
        ('Незакрытые или будущие назначения бригад',BrigadeAssignment.objects.filter(contract=contract).filter(Q(end_date__isnull=True)|Q(end_date__gt=today)).count()),
        ('Незакрытые или будущие назначения техники',EquipmentAssignment.objects.filter(contract=contract).filter(Q(end_date__isnull=True)|Q(end_date__gt=today)).count()),
    ]
    return [(label,count) for label,count in checks if count]


def pending_contracts(user):
    from django.db.models import OuterRef,Subquery
    return visible_contracts(user).annotate(last_closure=Subquery(ContractClosureEvent.objects.filter(contract_id=OuterRef('pk')).values('action')[:1])).filter(last_closure='request').exclude(status='archived')
