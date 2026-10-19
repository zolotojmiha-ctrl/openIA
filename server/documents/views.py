import csv
from uuid import UUID
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from accounts.models import User
from accounts.views import audit, leader_required
from projects.models import LegalEntity, COMPANY_TAX_ID
from projects.access import contract_card
from .models import Document, DocumentVersion, ApprovalEvent, DocumentAccess
from .forms import UploadForm, VersionForm, ApprovalForm, ShareForm
from .access import visible_documents, scoped_documents, has_permission, version_visible, can_download, can_modify
from .storage import store_file, private_path, verified_file


def listing(user, query):
    qs=visible_documents(user).select_related('current_version').order_by('-created_at')
    if query:qs=qs.filter(Q(title__icontains=query)|Q(number__icontains=query))
    return qs

@login_required
def documents(request):
    q=request.GET.get('q','').strip()[:200]
    archived=request.GET.get('archive')=='1'
    qs=listing(request.user,q).filter(archived=archived)
    return render(request,'documents/list.html',{'page':Paginator(qs,25).get_page(request.GET.get('page')),'q':q,'archived':archived,'upload_allowed':has_permission(request.user,'document.upload'),'export_allowed':has_permission(request.user,'document.export')})

@login_required
def upload(request):
    if not has_permission(request.user,'document.upload'):raise PermissionDenied
    form=UploadForm(request.POST or None,request.FILES or None,user=request.user)
    if request.method=='POST' and form.is_valid():
        key=None
        try:
            with transaction.atomic():
                user=User.objects.select_for_update().get(pk=request.user.pk)
                checked=UploadForm(request.POST,request.FILES,user=user)
                if not has_permission(user,'document.upload') or not checked.is_valid():raise PermissionDenied
                key=store_file(form.file_data)
                doc=form.save(commit=False)
                doc.created_by=user
                doc.legal_entity=get_object_or_404(LegalEntity,tax_id=COMPANY_TAX_ID)
                doc.save()
                doc.objects_linked.set(form.cleaned_data['linked_objects'])
                doc.contracts_linked.set(form.cleaned_data['linked_contracts'])
                doc.people_linked.set(form.cleaned_data['linked_people'])
                v=create_version(doc,user,form,key,1)
                doc.current_version=v;doc.save(update_fields=['current_version'])
                audit(user,'document_uploaded',entity_id=str(doc.pk),version_id=str(v.pk),sha256=v.sha256)
        except Exception:
            if key:private_path(key).unlink(missing_ok=True)
            raise
        messages.success(request,'Документ загружен и ожидает решения руководителя.')
        return redirect('document_detail',pk=doc.pk) if visible_documents(user).filter(pk=doc.pk).exists() else redirect('documents')
    return render(request,'documents/form.html',{'form':form,'title':'Загрузить документ'})


def create_version(doc,user,form,key,sequence):
    data=form.file_data
    return DocumentVersion.objects.create(document=doc,sequence=sequence,storage_key=key,original_name=data['name'],content_type=data['content_type'],size=data['size'],sha256=data['sha256'],uploaded_by=user,comment=form.cleaned_data.get('comment',''))

@login_required
def detail(request,pk):
    doc=get_object_or_404(visible_documents(request.user).select_related('current_version'),pk=pk)
    versions=[{'version':v,'download':can_download(request.user,doc,v)} for v in doc.versions.select_related('uploaded_by') if version_visible(request.user,doc,v)]
    return render(request,'documents/detail.html',{'document':doc,'versions':versions,'contracts':[contract_card(c,request.user) for c in doc.contracts_linked.all()],'objects':doc.objects_linked.all(),'replace_allowed':not doc.archived and can_modify(request.user,doc,'document.replace'),'archive_allowed':can_modify(request.user,doc,'document.restore' if doc.archived else 'document.archive')})

@login_required
def replace(request,pk):
    doc=get_object_or_404(visible_documents(request.user),pk=pk)
    if doc.archived or not can_modify(request.user,doc,'document.replace'):raise PermissionDenied
    form=VersionForm(request.POST or None,request.FILES or None,initial={'expected_revision':doc.revision})
    if request.method=='POST' and form.is_valid():
        key=None
        try:
            with transaction.atomic():
                user=User.objects.select_for_update().get(pk=request.user.pk)
                locked=get_object_or_404(Document.objects.select_for_update(),pk=pk)
                if not visible_documents(user).filter(pk=pk).exists():raise PermissionDenied
                if locked.archived or not can_modify(user,locked,'document.replace'):raise PermissionDenied
                if locked.revision!=form.cleaned_data['expected_revision']:
                    form.add_error(None,'Документ уже изменён. Обновите страницу.')
                else:
                    key=store_file(form.file_data)
                    v=create_version(locked,user,form,key,locked.versions.count()+1)
                    locked.current_version=v;locked.revision+=1;locked.save(update_fields=['current_version','revision'])
                    audit(user,'document_replaced',entity_id=str(pk),version_id=str(v.pk),sha256=v.sha256)
        except Exception:
            if key:private_path(key).unlink(missing_ok=True)
            raise
        if not form.errors:
            messages.success(request,'Новая версия сохранена. Ожидается утверждение руководителем.')
            return redirect('document_detail',pk=pk)
    return render(request,'documents/form.html',{'form':form,'title':'Новая версия · '+doc.title})

@leader_required
def approvals(request):
    qs=DocumentVersion.objects.filter(document__in=visible_documents(request.user),status='pending').select_related('document').order_by('created_at')
    return render(request,'documents/approvals.html',{'page':Paginator(qs,25).get_page(request.GET.get('page'))})

@leader_required
def approve(request,pk,version):
    doc=get_object_or_404(visible_documents(request.user),pk=pk)
    v=get_object_or_404(doc.versions,pk=version)
    form=ApprovalForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            Document.objects.select_for_update().get(pk=pk)
            v=DocumentVersion.objects.select_for_update().get(pk=v.pk)
            if v.status!='pending':
                return HttpResponse('Решение по этой версии уже принято. Обновите страницу.',status=409)
            ApprovalEvent.objects.create(version=v,actor=request.user,**form.cleaned_data)
            v.status=form.cleaned_data['decision'];v.save(update_fields=['status'])
            audit(request.user,'document_decided',entity_id=str(pk),version_id=str(v.pk),**form.cleaned_data)
        return redirect('document_detail',pk=pk)
    return render(request,'documents/approve.html',{'form':form,'document':doc,'version':v})

@login_required
@require_POST
def archive(request,pk):
    with transaction.atomic():
        user=User.objects.select_for_update().get(pk=request.user.pk)
        doc=get_object_or_404(Document.objects.select_for_update(),pk=pk)
        if not visible_documents(user).filter(pk=pk).exists():raise PermissionDenied
        if not can_modify(user,doc,'document.restore' if doc.archived else 'document.archive'):raise PermissionDenied
        if str(doc.revision)!=request.POST.get('expected_revision'):return HttpResponse('Документ уже изменён. Обновите страницу.',status=409)
        doc.archived=not doc.archived;doc.revision+=1;doc.save(update_fields=['archived','revision'])
        audit(user,'document_archived' if doc.archived else 'document_restored',entity_id=str(pk))
    return redirect('document_detail',pk=pk)

@login_required
def download(request,pk,version):
    doc=get_object_or_404(visible_documents(request.user),pk=pk)
    v=get_object_or_404(doc.versions,pk=version)
    if not can_download(request.user,doc,v):raise PermissionDenied
    try:stream=verified_file(v)
    except OSError:
        audit(request.user,'document_integrity_error',entity_id=str(pk),version_id=str(v.pk))
        return HttpResponse('Файл недоступен или не прошёл проверку сохранности. Обратитесь к руководителю.',status=503)
    audit(request.user,'document_downloaded',entity_id=str(pk),version_id=str(v.pk),status=v.status)
    response=FileResponse(stream,as_attachment=True,filename=v.original_name,content_type=v.content_type)
    response['X-Content-Type-Options']='nosniff'
    response['Content-Security-Policy']="sandbox; default-src 'none'"
    response['Cache-Control']='no-store'
    return response

@leader_required
def share(request,pk):
    doc=get_object_or_404(visible_documents(request.user),pk=pk)
    initial={'expected_revision':doc.revision}
    try:selected=UUID(request.GET.get('employee',''))
    except (ValueError,TypeError):selected=None
    if selected:
        initial['employee']=selected
        rule=doc.access_rules.filter(user_id=selected).first()
        if rule:initial.update(enabled=rule.enabled,can_download=rule.can_download,can_history=rule.can_history)
        elif doc.created_by_id==selected and doc.kind=='work':initial['enabled']=True
    form=ShareForm(request.POST or None,document=doc,initial=initial)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            user=User.objects.select_for_update().get(pk=form.cleaned_data['employee'].pk)
            doc=Document.objects.select_for_update().get(pk=pk)
            if not scoped_documents(user).filter(pk=pk).exists():raise PermissionDenied
            if doc.revision!=form.cleaned_data['expected_revision']:form.add_error(None,'Доступ уже изменён. Обновите страницу.')
            else:
                flags={name:form.cleaned_data[name] for name in ['enabled','can_download','can_history']}
                DocumentAccess.objects.update_or_create(document=doc,user=user,defaults={'granted_by':request.user,**flags})
                doc.revision+=1;doc.save(update_fields=['revision'])
                user.session_version+=1;user.save(update_fields=['session_version'])
                audit(request.user,'document_access_changed',user,entity_id=str(pk),**flags)
                messages.success(request,'Доступ сохранён. Прежние сеансы сотрудника завершены.')
                return redirect('document_detail',pk=pk)
    return render(request,'documents/share.html',{'form':form,'document':doc})

@login_required
def export(request):
    if not has_permission(request.user,'document.export'):raise PermissionDenied
    response=HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition']='attachment; filename="documents.csv"'
    response.write('\ufeff')
    writer=csv.writer(response)
    writer.writerow(['Название','Номер','Дата','Версия','Статус','Архив'])
    def safe(value):
        value=str(value or '')
        return "'"+value if value.lstrip().startswith(('=','+','-','@')) else value
    qs=listing(request.user,request.GET.get('q','').strip()[:200]).filter(archived=request.GET.get('archive')=='1')
    for doc in qs:
        v=doc.current_version
        writer.writerow([safe(doc.title),safe(doc.number),doc.date,v.sequence if v else '',v.get_status_display() if v else '',doc.archived])
    audit(request.user,'document_exported',format='csv',count=qs.count())
    return response
