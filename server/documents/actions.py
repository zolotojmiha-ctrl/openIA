"""Context-aware document actions. Deletion removes stored bytes, not audit facts."""
from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404,redirect,render
from django.urls import reverse
from django.utils import timezone
from accounts.models import User
from accounts.views import audit
from .models import Document,DocumentVersion,DocumentPart
from .access import visible_documents,visible_people,can_manage
from .storage import private_path

class NameForm(forms.Form):
    expected_revision=forms.IntegerField(widget=forms.HiddenInput)
    title=forms.CharField(label='Название документа',max_length=200)

class DeleteForm(forms.Form):
    expected_revision=forms.IntegerField(widget=forms.HiddenInput)
    confirm=forms.BooleanField(label='Подтверждаю удаление файла и всех его версий. Восстановление возможно только из резервной копии.')

def origin(request,doc):
    """Explicit context is verified against both the relationship and the caller ACL."""
    person_id=request.GET.get('person')
    if person_id:
        try:p=visible_people(request.user).filter(documents=doc,pk=person_id).first()
        except (ValidationError,ValueError):p=None
        if p:return reverse('person_detail',args=[p.pk]),'К документам сотрудника',{'person':str(p.pk)}
    equipment_id=request.GET.get('equipment')
    if equipment_id:
        from projects.equipment import visible_equipment
        try:e=visible_equipment(request.user).filter(file_links__document=doc,pk=equipment_id).first()
        except (ValidationError,ValueError):e=None
        if e:return reverse('equipment_detail',args=[e.pk]),'К документам техники',{'equipment':str(e.pk)}
    return reverse('documents'),'Документы',{}

def suffix(context):
    from urllib.parse import urlencode
    return '?'+urlencode(context) if context else ''

@login_required
def rename(request,pk):
    doc=get_object_or_404(visible_documents(request.user),pk=pk)
    if not can_manage(request.user,doc):raise PermissionDenied
    back,label,context=origin(request,doc)
    form=NameForm(request.POST or None,initial={'expected_revision':doc.revision,'title':doc.title})
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            actor=User.objects.select_for_update().get(pk=request.user.pk)
            if not actor.is_active or actor.session_version!=request.user.session_version:raise PermissionDenied
            doc=get_object_or_404(visible_documents(request.user).select_for_update(of=('self',)),pk=pk)
            if not can_manage(request.user,doc):raise PermissionDenied
            if doc.revision!=form.cleaned_data['expected_revision']:form.add_error(None,'Документ уже изменён. Обновите страницу.')
            else:
                old=doc.title;doc.title=form.cleaned_data['title'];doc.revision+=1;doc.save()
                audit(request.user,'document_links_changed',document=str(pk),kind='rename',previous=old,title=doc.title)
                return redirect(back if context else reverse('document_detail',args=[pk]))
    return render(request,'projects/operation_form.html',{'form':form,'title':'Переименовать документ','back':back})

@login_required
def delete(request,pk):
    doc=get_object_or_404(visible_documents(request.user),pk=pk)
    if not request.user.is_leader:raise PermissionDenied
    back,label,context=origin(request,doc)
    form=DeleteForm(request.POST or None,initial={'expected_revision':doc.revision})
    links={'Работники':[p.full_name for p in doc.personnel_linked.all()], 'Участки':[o.name for o in doc.objects_linked.all()], 'Контракты':[c.number for c in doc.contracts_linked.all()], 'Техника':[f.equipment.name for f in doc.equipment_links.select_related('equipment')]}
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            actor=User.objects.select_for_update().get(pk=request.user.pk)
            if not actor.is_active or actor.session_version!=request.user.session_version:raise PermissionDenied
            doc=get_object_or_404(visible_documents(request.user).select_for_update(of=('self',)),pk=pk)
            if doc.revision!=form.cleaned_data['expected_revision']:form.add_error(None,'Документ изменён. Обновите страницу.')
            elif doc.versions.filter(admissionfile__isnull=False).exists():form.add_error(None,'Документ включён в сохранённую редакцию допуска. Удаление нарушит комплект. Можно убрать отдельную связь с карточкой или архивировать документ.')
            else:
                versions=list(doc.versions.prefetch_related('parts'))
                keys={v.storage_key for v in versions}|{p.storage_key for v in versions for p in v.parts.all()}
                shared=set(DocumentVersion.objects.filter(storage_key__in=keys,document__deleted_at__isnull=True).exclude(document=doc).values_list('storage_key',flat=True))
                shared.update(DocumentPart.objects.filter(storage_key__in=keys,version__document__deleted_at__isnull=True).exclude(version__document=doc).values_list('storage_key',flat=True))
                keys-=shared
                # Archive guard rejects destruction of files linked to closed contracts.
                doc.deleted_at=timezone.now();doc.archived=True;doc.revision+=1;doc.save()
                def purge():
                    for key in keys:private_path(key).unlink(missing_ok=True)
                transaction.on_commit(purge)
                audit(request.user,'document_deleted',document=str(pk),kind='deleted',title=doc.title,versions=len(versions),links=links)
                return redirect(back)
    return render(request,'projects/operation_form.html',{'form':form,'title':'Удалить документ · '+doc.title,'back':back,'intro':'Удаляются файл и все версии во всех карточках. Для удаления только связи используйте «Убрать из карточки». Общие исходники, используемые другими документами, сохраняются для них. Метаданные и журнал действий сохраняются.','link_summary':links})

class UnlinkForm(forms.Form):
    expected_revision=forms.IntegerField(widget=forms.HiddenInput)
    confirm=forms.BooleanField(label='Убрать документ только из этой карточки сотрудника')

@login_required
def unlink_person(request,pk,person_id):
    doc=get_object_or_404(visible_documents(request.user),pk=pk)
    person=get_object_or_404(visible_people(request.user).filter(documents=doc),pk=person_id)
    if not can_manage(request.user,doc):raise PermissionDenied
    form=UnlinkForm(request.POST or None,initial={'expected_revision':doc.revision})
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            doc=Document.objects.select_for_update().get(pk=pk)
            if doc.revision!=form.cleaned_data['expected_revision']:form.add_error(None,'Документ уже изменён.')
            else:
                doc.revision+=1;doc.save();doc.personnel_linked.remove(person)
                audit(request.user,'document_links_changed',document=str(pk),kind='unlink_person',person=str(person.pk));return redirect('person_detail',pk=person.pk)
    return render(request,'projects/operation_form.html',{'form':form,'title':'Убрать документ из карточки','back':reverse('person_detail',args=[person.pk])})
