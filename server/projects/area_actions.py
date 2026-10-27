from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404,redirect,render
from django.urls import reverse
from accounts.views import leader_required,audit
from .access import visible_objects
from .models import ProjectObject,ContractObject,ObjectAccess
from repairs.models import Section

class DeleteForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    confirm=forms.BooleanField(label='Удалить ошибочно созданный участок')

def blockers(obj):
    rows=[]
    labels={'documents.documentobject':'Документы участка','documents.personassignment':'Назначения работников','projects.brigadeassignment':'Назначения бригад','projects.equipmentassignment':'Назначения техники','repairs.job':'Работы по дефектам','repairs.defect':'Дефекты','repairs.importbatch':'Загруженные ведомости','projects.admission':'Комплекты допуска','projects.customerpublication':'Публикации заказчику','projects.customeraccess':'Доступ заказчика','projects.materialrequest':'Потребность в материалах'}
    for rel in obj._meta.related_objects:
        if rel.many_to_many or rel.related_model in (ContractObject,ObjectAccess,Section):continue
        count=rel.related_model._base_manager.filter(**{rel.field.name:obj}).count()
        if count:rows.append((labels.get(rel.related_model._meta.label_lower,str(rel.related_model._meta.verbose_name)),count))
    for section in Section.objects.filter(project_object=obj):
        for rel in section._meta.related_objects:
            if not rel.many_to_many:
                count=rel.related_model._base_manager.filter(**{rel.field.name:section}).count()
                if count:rows.append((labels.get(rel.related_model._meta.label_lower,str(rel.related_model._meta.verbose_name)),count))
    return rows

@leader_required
def delete_area(request,pk):
    obj=get_object_or_404(visible_objects(request.user),pk=pk)
    back=reverse('contract_detail',args=[obj.contracts.first().pk]) if obj.contracts.exists() else reverse('contracts')
    form=DeleteForm(request.POST or None,initial={'expected_version':obj.version})
    dependencies=blockers(obj)
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                from .operations import lock_actor
                lock_actor(request.user)
                obj=ProjectObject.objects.select_for_update().get(pk=pk)
                if obj.version!=form.cleaned_data['expected_version']:raise ValidationError('Участок уже изменён. Обновите страницу.')
                dependencies=blockers(obj)
                if dependencies:raise ValidationError('У участка есть связанные записи. Перенесите их или используйте архивирование; история не удаляется автоматически.')
                Section.objects.filter(project_object=obj).delete()
                ObjectAccess.objects.filter(project_object=obj).delete()
                ContractObject.objects.filter(project_object=obj).delete()
                title=obj.name;obj.delete();audit(request.user,'object_deleted',entity_id=str(pk),kind='deleted',title=title)
        except (ValidationError,ProtectedError) as e:form.add_error(None,e if isinstance(e,ValidationError) else 'Есть связанные записи. Удаление отменено.')
        else:messages.success(request,'Пустой участок удалён.');return redirect(back)
    return render(request,'projects/operation_form.html',{'form':form,'title':'Удалить участок · '+obj.name,'back':back,'dependencies':dependencies,'intro':'Удаление пустого участка доступно руководителям. Связанные документы, задания, назначения и результаты автоматически не удаляются.'})
