from accounts import xlsx as human_xlsx
import uuid
from django import forms
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render,redirect
from accounts.models import User
from documents.models import Person
from repairs.excel import workbook
from repairs.importer import read_book
from .access import boxes,leader
from .models import Mailbox,MailboxImportReceipt
from . import services
HEADERS=['ID ящика','Адрес','ID учётной записи','ID сотрудника','Состояние (requested/active/disabled)','Версия']
class UploadForm(forms.Form):file=forms.FileField(label='Реестр почтовых ящиков XLSX')

def apply(user,rows):
    admin=services.actor(user);leader(admin)
    for number,v in rows:
        try:
            owner=User.objects.get(pk=v[2],is_active=True) if v[2] else None
            person=Person.objects.get(pk=v[3],legal_entity__tax_id='8604073687',archived=False) if v[3] else None
            if v[4] not in ['requested','active','disabled']:raise ValidationError('Проверьте состояние ящика.')
            if v[0]:
                box=Mailbox.objects.get(pk=v[0])
                if box.address!=v[1].lower():raise ValidationError('Адрес существующего ящика не переименовывается через Excel.')
                if v[4]=='requested':
                    if box.state!='requested' or str(box.version)!=v[5] or box.owner_id!=getattr(owner,'pk',None) or box.person_id!=getattr(person,'pk',None):raise ValidationError('Изменяйте подключение в карточке ящика.')
                else:services.configure(admin,box.pk,int(v[5]),'',owner,person,v[4]=='active')
            else:
                if v[4]!='requested':raise ValidationError('Новый адрес импортируется только как заявка, без пароля.')
                services.request_address(admin,v[1],owner,person)
        except (ValueError,User.DoesNotExist,Person.DoesNotExist,Mailbox.DoesNotExist,ValidationError) as e:raise ValidationError(f'Строка {number}: '+('; '.join(e.messages) if isinstance(e,ValidationError) else 'Проверьте ID и версию.')) from e

@login_required
def registry(request):
    leader(request.user)
    if request.GET.get('export')=='1' or request.GET.get('template')=='1':
        rows=[] if request.GET.get('template')=='1' else [[str(b.pk),b.address,str(b.owner_id or ''),str(b.person_id or ''),b.state,b.version] for b in boxes(request.user)]
        response=HttpResponse(human_xlsx.workbook(request.user,'mail',rows,HEADERS),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');response['Content-Disposition']='attachment; filename="armada-mailboxes.xlsx"';return response
    form=UploadForm(request.POST or None,request.FILES or None);preview='';rows=[]
    if request.method=='POST':
        try:
            if request.POST.get('apply')=='1':
                payload=signing.loads(request.POST.get('preview',''),salt='mail-excel',max_age=3600)
                if payload['actor']!=str(request.user.pk) or payload['session']!=request.user.session_version:raise PermissionDenied
                with transaction.atomic():
                    actor=services.actor(request.user);leader(actor)
                    if not MailboxImportReceipt.objects.filter(token=payload['token'],actor=actor).exists():apply(actor,payload['rows']);MailboxImportReceipt.objects.create(token=payload['token'],actor=actor)
                return redirect('mail')
            if form.is_valid():
                book=human_xlsx.normalize(request.user,'mail',read_book(form.cleaned_data['file']),HEADERS);cells=book['cells']
                if book['formulas']:raise ValidationError('Формулы замените значениями.')
                if [str(cells.get((1,i+1),'')).strip() for i in range(len(HEADERS))]!=HEADERS:raise ValidationError('Используйте шаблон реестра почты.')
                seen=set()
                for n in sorted({r for r,c in cells if r>1}):
                    v=[str(cells.get((n,i+1),'')).strip() for i in range(len(HEADERS))]
                    if not any(v):continue
                    if len(rows)>=500 or v[1].lower() in seen:raise ValidationError('Не более 500 строк, без повторных адресов.')
                    seen.add(v[1].lower());rows.append([n,v])
                if not rows:raise ValidationError('Нет строк для переноса.')
                with transaction.atomic():apply(request.user,rows);transaction.set_rollback(True)
                preview=signing.dumps({'actor':str(request.user.pk),'session':request.user.session_version,'token':str(uuid.uuid4()),'rows':rows},salt='mail-excel',compress=True)
        except (signing.BadSignature,ValidationError,KeyError) as e:form.add_error(None,e if isinstance(e,ValidationError) else 'Предпросмотр устарел. Загрузите файл заново.')
    return render(request,'postoffice/excel.html',{'form':form,'preview':preview,'rows':rows})
