"""Explicit, leader-only deletion; never cascade through work history."""
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404,render,redirect
from django.urls import reverse
from accounts.views import leader_required,audit
from .models import Contract,CounterpartyAgreement,ContractObject,ContractAccess
from .access import visible_contracts
from .operations import lock_actor
from .archive import lifecycle_write
from documents.models import DocumentContract

class DeleteForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    confirm=forms.BooleanField(label='Подтверждаю удаление записи. Прикреплённые файлы сохраняются в документах.')

def dependencies(row):
    result=[]
    for relation in row._meta.related_objects:
        if relation.many_to_many or relation.related_model in (ContractObject,ContractAccess,DocumentContract):continue
        count=relation.related_model._base_manager.filter(**{relation.field.name:row}).count()
        if count:result.append((str(relation.related_model._meta.verbose_name),count))
    return result

@leader_required
def delete(request,pk,agreement=False):
    model=CounterpartyAgreement if agreement else Contract
    scope=model.objects.all() if agreement else visible_contracts(request.user)
    row=get_object_or_404(scope,pk=pk)
    back=reverse('agreement_detail' if agreement else 'contract_detail',args=[pk])
    if request.GET.get('from')=='list':back=reverse('agreements' if agreement else 'contracts')
    form=DeleteForm(request.POST or None,initial={'expected_version':row.version})
    related=dependencies(row)
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic(),lifecycle_write():
                actor=lock_actor(request.user)
                row=model.objects.select_for_update().get(pk=pk)
                if row.version!=form.cleaned_data['expected_version']:raise ValidationError('Запись изменилась. Обновите страницу.')
                related=dependencies(row)
                if related:raise ValidationError('Есть связанные работы или записи. Удаление отменено: сначала перенесите связи или используйте архив. История не удаляется автоматически.')
                title=row.number
                if not agreement:
                    if row.status=='archived':
                        row.status='planned';row.save(update_fields=['status'])
                    DocumentContract.objects.filter(contract=row).delete()
                    ContractObject.objects.filter(contract=row).delete()
                    ContractAccess.objects.filter(contract=row).delete()
                row.delete()
                audit(request.user,'agreement_updated' if agreement else 'contract_updated',entity_id=str(pk),kind='deleted',title=title)
        except (ValidationError,ProtectedError) as exc:form.add_error(None,exc if isinstance(exc,ValidationError) else 'Есть связанные записи. Удаление отменено.')
        else:
            messages.success(request,'Договор удалён. Файлы и участки сохранены.')
            return redirect('agreements' if agreement else 'contracts')
    return render(request,'projects/operation_form.html',{'title':('Удалить договор · ' if agreement else 'Удалить контракт · ')+row.number,'form':form,'back':back,'dependencies':related,'intro':'Удаляется карточка договора. Самостоятельные участки и файлы сохраняются. При наличии работ, финансовых записей или иной истории удаление блокируется с перечнем связей.'})
