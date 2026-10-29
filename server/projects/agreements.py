from accounts import xlsx as human_xlsx
"""Leader-only supporting agreement register; no financial postings are created."""
import uuid
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.shortcuts import render,redirect,get_object_or_404
from django.http import HttpResponse
from django.core.paginator import Paginator
from accounts.models import User,AuditEvent
from accounts.views import audit
from documents.access import visible_documents
from .access import visible_contracts,visible_objects
from .equipment import visible_equipment
from .models import CounterpartyAgreement,AgreementImportReceipt

FIELDS=['number','kind','counterparty','subject','date','deadline','amount','customer_contract','project_object','equipment','notes','archived']
HEADERS=['ID','Версия','Номер','Вид (subcontract/equipment_rent/housing/supply/other)','Контрагент','Предмет','Дата (ГГГГ-ММ-ДД)','Срок (ГГГГ-ММ-ДД)','Сумма, ₽','ID контракта заказчика','ID объекта','ID техники','Примечания','Архив (0/1)']
def guard(user):
    if not user.is_active or not user.is_leader:raise PermissionDenied

class AgreementForm(forms.ModelForm):
    expected_version=forms.IntegerField(widget=forms.HiddenInput,min_value=0)
    class Meta:
        model=CounterpartyAgreement
        fields=FIELDS+['documents']
        widgets={'date':forms.DateInput(attrs={'type':'date'},format='%Y-%m-%d'),'deadline':forms.DateInput(attrs={'type':'date'},format='%Y-%m-%d'),'notes':forms.Textarea(attrs={'rows':3})}
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['customer_contract'].queryset=visible_contracts(user)
        self.fields['project_object'].queryset=visible_objects(user)
        self.fields['equipment'].queryset=visible_equipment(user)
        self.fields['documents'].queryset=visible_documents(user)
        self.fields['documents'].help_text='Сначала загрузите файлы в «Документы», затем выберите их здесь. Связь не выдаёт дополнительных прав доступа.'

@transaction.atomic
def save(user,pk,data,new_id=None):
    actor=User.objects.select_for_update().get(pk=user.pk);guard(actor)
    if actor.session_version!=user.session_version:raise PermissionDenied
    row=get_object_or_404(CounterpartyAgreement.objects.select_for_update(),pk=pk) if pk else CounterpartyAgreement(pk=new_id or uuid.uuid4(),created_by=actor)
    before={k:str(getattr(row,k)) for k in FIELDS} if pk else {}
    form=AgreementForm(data,user=actor,instance=row)
    if not form.is_valid():raise ValidationError([str(e) for errors in form.errors.values() for e in errors])
    expected=1 if not pk else row.version
    if form.cleaned_data['expected_version']!=(row.version if pk else 0):raise ValidationError('Карточка изменилась. Обновите страницу.')
    row=form.save(commit=False);row.version=expected+1 if pk else 1;row.save();form.save_m2m()
    audit(actor,'agreement_updated',agreement=str(row.pk),before=before,after={k:str(getattr(row,k)) for k in FIELDS},documents=[str(p) for p in row.documents.values_list('pk',flat=True)])
    return row

@login_required
def home(request):
    guard(request.user);q=request.GET.get('q','').strip()[:200]
    rows=CounterpartyAgreement.objects.filter(archived=request.GET.get('archive')=='1').select_related('customer_contract','project_object').order_by('-updated_at')
    if q:rows=rows.filter(Q(number__icontains=q)|Q(counterparty__icontains=q)|Q(subject__icontains=q))
    if request.GET.get('kind'):rows=rows.filter(kind=request.GET['kind'])
    if request.GET.get('contract'):
        try:rows=rows.filter(customer_contract_id=uuid.UUID(request.GET['contract']))
        except ValueError:rows=rows.none()
    return render(request,'projects/agreements.html',{'page':Paginator(rows,30).get_page(request.GET.get('page')),'q':q,'kinds':CounterpartyAgreement.KINDS})

@login_required
def edit(request,pk=None):
    guard(request.user);row=get_object_or_404(CounterpartyAgreement,pk=pk) if pk else None
    form=AgreementForm(request.POST or None,user=request.user,instance=row,initial={'expected_version':row.version if row else 0})
    if request.method=='POST' and form.is_valid():
        try:result=save(request.user,pk,request.POST)
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('agreement_detail',pk=result.pk)
    return render(request,'projects/agreement_form.html',{'form':form,'row':row})

@login_required
def detail(request,pk):
    guard(request.user);row=get_object_or_404(CounterpartyAgreement,pk=pk)
    events=list(AuditEvent.objects.filter(action='agreement_updated',details__agreement=str(row.pk)).select_related('actor').order_by('-created_at')[:50])
    labels={field.name:str(field.verbose_name) for field in CounterpartyAgreement._meta.fields}
    for event in events:event.change_rows=[(labels.get(k,k),dict(CounterpartyAgreement.KINDS).get(v,v) if k=='kind' else ('Да' if v=='True' else 'Нет') if k=='archived' else 'Не указано' if v=='None' else v) for k,v in event.details.get('after',{}).items()]
    return render(request,'projects/agreement_detail.html',{'row':row,'documents':row.documents.filter(pk__in=visible_documents(request.user)),'events':events[:50]})

class UploadForm(forms.Form):file=forms.FileField(label='Договоры XLSX')

def apply(user,rows):
    # Actor lock serializes receipts, edits and complete workbook application.
    actor=User.objects.select_for_update().get(pk=user.pk);guard(actor)
    for number,values in rows:
        data=dict(zip(FIELDS,values[2:]));data['expected_version']=values[1] or '0'
        data['archived']=values[-1]=='1'
        if values[-1] not in ('','0','1'):raise ValidationError(f'Строка {number}: Архив — 0 или 1.')
        row=CounterpartyAgreement.objects.filter(pk=values[0]).first()
        if row:data['documents']=[str(pk) for pk in row.documents.values_list('pk',flat=True)]
        try:
            save(user,row.pk if row else None,data,new_id=values[0])
        except ValidationError as e:raise ValidationError(f'Строка {number}: '+ '; '.join(e.messages)) from e

@login_required
def excel(request):
    guard(request.user)
    from repairs.excel import workbook
    from repairs.importer import read_book
    if request.GET.get('export')=='1' or request.GET.get('template')=='1':
        rows=[]
        if request.GET.get('export')=='1':
            for r in CounterpartyAgreement.objects.order_by('number'):
                values=[str(r.pk),r.version]
                for field in FIELDS:
                    value=getattr(r,field+'_id') if field in ('customer_contract','project_object','equipment') else getattr(r,field)
                    values.append(('1' if value else '0') if field=='archived' else str(value) if value is not None else '')
                rows.append(values)
        response=HttpResponse(human_xlsx.workbook(request.user,'agreements',rows,HEADERS),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');response['Content-Disposition']='attachment; filename="armada-counterparty-agreements.xlsx"';return response
    form=UploadForm(request.POST or None,request.FILES or None);preview='';rows=[]
    if request.method=='POST':
        try:
            if request.POST.get('apply')=='1':
                payload=signing.loads(request.POST.get('preview',''),salt='agreement-excel',max_age=3600)
                if payload['actor']!=str(request.user.pk) or payload['session']!=request.user.session_version:raise PermissionDenied
                with transaction.atomic():
                    actor=User.objects.select_for_update().get(pk=request.user.pk);guard(actor)
                    if actor.session_version!=request.user.session_version:raise PermissionDenied
                    if not AgreementImportReceipt.objects.filter(token=payload['token']).exists():apply(actor,payload['rows']);AgreementImportReceipt.objects.create(token=payload['token'],actor=actor)
                return redirect('agreements')
            if form.is_valid():
                book=human_xlsx.normalize(request.user,'agreements',read_book(form.cleaned_data['file']),HEADERS);cells=book['cells']
                if book['formulas']:raise ValidationError('Замените формулы значениями.')
                if [str(cells.get((1,i+1),'')).strip() for i in range(len(HEADERS))]!=HEADERS:raise ValidationError('Используйте шаблон договоров с контрагентами.')
                seen=set()
                for n in sorted({r for r,c in cells if r>1}):
                    values=[str(cells.get((n,i+1),'')).strip() for i in range(len(HEADERS))]
                    if not any(values):continue
                    if len(rows)>=500:raise ValidationError('Не более 500 строк.')
                    if values[0]:
                        try:key=str(uuid.UUID(values[0]))
                        except ValueError:raise ValidationError(f'Строка {n}: неверный ID.')
                        if not CounterpartyAgreement.objects.filter(pk=key).exists():raise ValidationError(f'Строка {n}: договор с таким ID не найден. Для новой записи оставьте ID пустым.')
                    else:key=str(uuid.uuid4())
                    if key in seen:raise ValidationError('Повторный ID договора.')
                    seen.add(key);values[0]=key;rows.append([n,values])
                if not rows:raise ValidationError('Нет строк для переноса.')
                with transaction.atomic():apply(request.user,rows);transaction.set_rollback(True)
                preview=signing.dumps({'actor':str(request.user.pk),'session':request.user.session_version,'token':str(uuid.uuid4()),'rows':rows},salt='agreement-excel',compress=True)
        except (ValidationError,signing.BadSignature,KeyError) as e:form.add_error(None,e if isinstance(e,ValidationError) else 'Предпросмотр устарел. Загрузите файл заново.')
    return render(request,'projects/agreement_excel.html',{'form':form,'preview':preview,'rows':rows})
