"""Optional document expiry monitoring, always bounded by the file ACL."""
from datetime import timedelta,date
import uuid
from django import forms
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied,ValidationError,ObjectDoesNotExist
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse,Http404
from django.shortcuts import get_object_or_404,redirect,render
from django.utils import timezone
from accounts.models import User
from accounts.views import audit
from repairs.excel import workbook
from repairs.importer import read_book
from .models import Document,DocumentDeadlineBatch
from .access import visible_documents,can_manage,has_permission

class ExpiryForm(forms.ModelForm):
    expected_revision=forms.IntegerField(widget=forms.HiddenInput)
    class Meta:
        model=Document
        fields=['expires_on','remind','days_before']
        widgets={'expires_on':forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'})}
    def clean_days_before(self):
        days=self.cleaned_data['days_before']
        if not 0<=days<=365:raise ValidationError('Укажите от 0 до 365 дней.')
        return days
    def clean(self):
        data=super().clean()
        if data.get('remind') and not data.get('expires_on'):self.add_error('expires_on','Для напоминания нужна дата окончания.')
        return data

def due_documents(user):
    today=timezone.localdate()
    return [doc for doc in visible_documents(user).filter(archived=False,remind=True,expires_on__isnull=False,expires_on__lte=today+timedelta(days=365)).order_by('expires_on','pk') if doc.expires_on<=today+timedelta(days=doc.days_before)]

@transaction.atomic
def save_expiry(user,pk,version,expires,remind,days):
    actor=User.objects.select_for_update(no_key=True).get(pk=user.pk)
    if not actor.is_active or actor.session_version!=user.session_version:raise PermissionDenied
    doc=get_object_or_404(Document.objects.select_for_update(),pk=pk)
    if not visible_documents(actor).filter(pk=pk).exists() or not can_manage(actor,doc) or doc.archived:raise PermissionDenied
    if doc.revision!=version:raise ValidationError('Документ или срок уже изменён. Обновите страницу.')
    if not 0<=days<=365 or remind and not expires:raise ValidationError('Для напоминания укажите дату и от 0 до 365 дней.')
    before={'expires':str(doc.expires_on or ''),'remind':doc.remind,'days':doc.days_before}
    doc.expires_on=expires;doc.remind=remind;doc.days_before=days;doc.revision+=1
    doc.save(update_fields=['expires_on','remind','days_before','revision'])
    audit(actor,'document_expiry_changed',entity_id=str(doc.pk),kind='expiry',previous=before,expires=str(expires or ''),remind=remind,days=days)
    return doc

@login_required
def configure(request,pk):
    doc=get_object_or_404(visible_documents(request.user),pk=pk)
    if not can_manage(request.user,doc) or doc.archived:raise PermissionDenied
    form=ExpiryForm(request.POST or None,instance=doc,initial={'expected_revision':doc.revision})
    if request.method=='POST' and form.is_valid():
        try:save_expiry(request.user,pk,form.cleaned_data['expected_revision'],form.cleaned_data['expires_on'],form.cleaned_data['remind'],form.cleaned_data['days_before'])
        except ValidationError as error:form.add_error(None,error)
        else:return redirect('document_detail',pk=pk)
    return render(request,'documents/expiry_form.html',{'form':form,'document':doc})

@login_required
def listing(request):
    due=due_documents(request.user);qs=visible_documents(request.user).filter(archived=False)
    if request.GET.get('all')=='1':qs=qs.filter(Q(expires_on__isnull=False)|Q(remind=True))
    else:qs=qs.filter(pk__in=[d.pk for d in due])
    page=Paginator(qs.order_by('expires_on','pk'),25).get_page(request.GET.get('page'))
    rows=[];today=timezone.localdate()
    for doc in page:
        rows.append({'document':doc,'manage':can_manage(request.user,doc),'remaining':(doc.expires_on-today).days if doc.expires_on else None})
    return render(request,'documents/deadlines.html',{'rows':rows,'page':page,'all':request.GET.get('all')=='1','due_count':len(due),'can_export_deadlines':has_permission(request.user,'document.export'),'can_import_deadlines':request.user.is_leader or visible_documents(request.user).filter(Q(created_by=request.user)|Q(responsible=request.user)).exists()})

HEADERS=['ID документа','Название (для справки)','Действует до (ГГГГ-ММ-ДД)','Напоминать (0/1)','За сколько дней','Версия карточки']
@login_required
def export(request):
    if not has_permission(request.user,'document.export'):raise PermissionDenied
    rows=[[str(d.pk),d.title,str(d.expires_on or ''),int(d.remind),d.days_before,d.revision] for d in visible_documents(request.user).filter(archived=False).order_by('pk')]
    response=HttpResponse(workbook(rows,headers=HEADERS),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition']='attachment; filename="armada-document-deadlines.xlsx"';response['Cache-Control']='no-store'
    audit(request.user,'document_exported',format='xlsx',kind='deadlines',count=len(rows));return response

class UploadForm(forms.Form):file=forms.FileField(label='Сроки документов XLSX')

def rows_from_file(file):
    book=read_book(file);cells=book['cells']
    if book['formulas']:raise ValidationError('Вставьте значения вместо формул.')
    if [str(cells.get((1,i+1),'')).strip() for i in range(len(HEADERS))]!=HEADERS:raise ValidationError('Используйте столбцы из выгрузки сроков.')
    result=[];seen=set()
    for n in sorted({r for r,c in cells if r>1}):
        values=[str(cells.get((n,i+1),'')).strip() for i in range(len(HEADERS))]
        if not any(values):continue
        if len(result)>=500:raise ValidationError('За один импорт — до 500 строк.')
        try:
            pk=str(uuid.UUID(values[0]));raw_date=values[2]
            if raw_date and raw_date.replace('.','',1).isdigit():
                serial=float(raw_date)
                if not 0<serial<200000 or int(serial)!=serial:raise ValueError
                expires=(book['epoch']+timedelta(days=int(serial))).isoformat()
            else:expires=date.fromisoformat(raw_date).isoformat() if raw_date else ''
            if pk in seen or values[3] not in ['0','1'] or not 0<=int(values[4])<=365 or values[3]=='1' and not expires:raise ValueError
            seen.add(pk);result.append({'id':pk,'expires':expires,'remind':values[3]=='1','days':int(values[4]),'version':int(values[5]),'row':n})
        except (ValueError,TypeError):raise ValidationError(f'Строка {n}: проверьте ID, дату, флаг 0/1, дни и версию.')
    if not result:raise ValidationError('Нет строк для переноса.')
    return result

@transaction.atomic
def apply_rows(user,rows):
    for row in sorted(rows,key=lambda r:r['id']):
        try:save_expiry(user,row['id'],row['version'],date.fromisoformat(row['expires']) if row['expires'] else None,row['remind'],row['days'])
        except (ValidationError,ObjectDoesNotExist,Http404) as error:raise ValidationError(f'Строка {row["row"]}: проверьте существующий документ, доступ, даты и версию.') from error

@login_required
def upload(request):
    form=UploadForm(request.POST or None,request.FILES or None);rows=None;preview=''
    if request.method=='POST':
        try:
            if request.POST.get('apply')=='1':
                payload=signing.loads(request.POST.get('preview',''),salt='document-deadlines',max_age=3600)
                if payload['actor']!=str(request.user.pk):raise PermissionDenied
                with transaction.atomic():
                    actor=User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
                    if not actor.is_active or actor.session_version!=request.user.session_version:raise PermissionDenied
                    prior=DocumentDeadlineBatch.objects.filter(token=payload['token']).first()
                    if prior and prior.actor_id!=actor.pk:raise PermissionDenied
                    if not prior:
                        apply_rows(actor,payload['rows']);DocumentDeadlineBatch.objects.create(actor=actor,token=payload['token'])
                return redirect('document_deadlines')
            elif form.is_valid():
                rows=rows_from_file(form.cleaned_data['file'])
                with transaction.atomic():apply_rows(request.user,rows);transaction.set_rollback(True)
                preview=signing.dumps({'actor':str(request.user.pk),'token':str(uuid.uuid4()),'rows':rows},salt='document-deadlines',compress=True)
        except signing.BadSignature:form.add_error(None,'Предварительный просмотр устарел. Проверьте файл заново.')
        except ValidationError as error:form.add_error(None,error);rows=None
    return render(request,'documents/deadlines_import.html',{'form':form,'rows':rows,'preview':preview,'can_export_deadlines':has_permission(request.user,'document.export')})
