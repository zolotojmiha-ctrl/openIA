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

class ClosureForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    action=forms.ChoiceField(label='Действие',choices=[])
    note=forms.CharField(label='Пояснение',max_length=5000,widget=forms.Textarea)
    def __init__(self,*args,choices,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['action'].choices=choices

def choices(user,contract):
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
    if action=='archive':contract.status='archived'
    elif action=='restore':
        archived=contract.closure_events.filter(action='archive').first()
        contract.status=archived.previous_status if archived and archived.previous_status!='archived' else 'active'
    contract.version+=1
    contract.save(update_fields=['status','version','updated_at'])
    ContractClosureEvent.objects.create(contract=contract,actor=actor,action=action,note=note.strip(),previous_status=old)
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
    return render(request,'projects/closure.html',{'form':form,'card':contract_card(contract,request.user)})
