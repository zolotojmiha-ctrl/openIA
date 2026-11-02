from accounts import xlsx as human_xlsx
import uuid,hashlib,json
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
from documents.models import Document
from documents.services import make_version
from documents.storage import validate_upload,store_file,private_path
from finance.money_forms import MoneyModelForm
from finance.templatetags.finance_format import rub
from .scope_widgets import ScopeChecklist,ScopedContracts,identifiers
from .access import visible_contracts,visible_objects
from .equipment import visible_equipment
from .models import CounterpartyAgreement,AgreementImportReceipt,LegalEntity,COMPANY_TAX_ID

FIELDS=['number','kind','counterparty','subject','date','deadline','amount','customer_contract','project_object','equipment','notes','archived']
HEADERS=['ID','Версия','Номер','Вид (subcontract/equipment_rent/housing/supply/other)','Контрагент','Предмет','Дата (ГГГГ-ММ-ДД)','Срок (ГГГГ-ММ-ДД)','Сумма, ₽','ID контракта заказчика','ID объекта','ID техники','Примечания','Архив (0/1)']
EXTRA_HEADERS=['ID дополнительных контрактов','ID дополнительных объектов']
def guard(user):
    if not user.is_active or not user.is_leader:raise PermissionDenied

class MultipleFiles(forms.ClearableFileInput):allow_multiple_selected=True
class AgreementForm(MoneyModelForm):
    expected_version=forms.IntegerField(widget=forms.HiddenInput,min_value=0)
    submission_token=forms.UUIDField(widget=forms.HiddenInput,required=False,initial=uuid.uuid4)
    customer_contracts=ScopedContracts(label='Контракты заказчиков',queryset=None,required=False,widget=ScopeChecklist)
    project_objects=forms.ModelMultipleChoiceField(label='Объекты',queryset=None,required=False,widget=ScopeChecklist)
    class Meta:
        model=CounterpartyAgreement
        fields=[f for f in FIELDS if f not in ('customer_contract','project_object')]+['customer_contracts','project_objects','documents']
        widgets={'date':forms.DateInput(attrs={'type':'date'},format='%Y-%m-%d'),'deadline':forms.DateInput(attrs={'type':'date'},format='%Y-%m-%d'),'notes':forms.Textarea(attrs={'rows':3}),'documents':ScopeChecklist}
    def __init__(self,*args,user,**kwargs):
        args=list(args)
        if args and args[0] is not None:
            data=args[0].copy()
            for old,new in [('customer_contract','customer_contracts'),('project_object','project_objects')]:
                if old in data and new not in data:
                    values=[data[old]] if data[old] else []
                    if hasattr(data,'setlist'):data.setlist(new,values)
                    else:data[new]=values
            args[0]=data
        super().__init__(*args,**kwargs)
        self.fields['customer_contracts'].queryset=visible_contracts(user);self.fields['customer_contracts'].user=user
        self.fields['project_objects'].queryset=visible_objects(user)
        self.fields['equipment'].queryset=visible_equipment(user)
        # Existing links stay visible; searching new files happens on demand.
        available=visible_documents(user)
        query=str(self.data.get('document_search','') if self.is_bound else self.initial.get('document_search','')).strip()[:150]
        selected=self.data.getlist('documents') if hasattr(self.data,'getlist') else self.data.get('documents',[])
        valid=[]
        for value in selected or []:
            try:valid.append(uuid.UUID(str(value)))
            except (ValueError,TypeError):pass
        selected=valid
        linked=self.instance.documents.values('pk') if self.instance.pk else []
        allowed=Q(pk__in=linked)|Q(pk__in=selected or [])
        matches=list(available.filter(Q(title__icontains=query)|Q(current_version__original_name__icontains=query)).order_by('title').values_list('pk',flat=True).distinct()[:51]) if query else []
        self.document_search_count=min(len(matches),50)
        self.document_search_more=len(matches)>50
        if query:allowed|=Q(pk__in=matches[:50])
        self.fields['documents'].queryset=available.filter(allowed).distinct().order_by('title')
        self.fields['documents'].label='Прикреплённые и выбранные файлы'
        self.fields['documents'].help_text=''
    def clean(self):
        data=super().clean();contracts=data.get('customer_contracts');objects=data.get('project_objects')
        if contracts and objects:
            bad=objects.exclude(contracts__in=contracts)
            if bad.exists():self.add_error('project_objects','Каждый объект должен входить хотя бы в один выбранный контракт.')
        # Legacy singular fields are maintained as a compatible primary pair at save.
        self.instance.customer_contract=None;self.instance.project_object=None
        return data

@transaction.atomic
def save(user,pk,data,new_id=None,uploads=()):
    actor=User.objects.select_for_update(no_key=True).get(pk=user.pk);guard(actor)
    if actor.session_version!=user.session_version:raise PermissionDenied
    if len(uploads)>20:raise ValidationError('За один раз можно прикрепить до 20 файлов.')
    prepared=[validate_upload(f) for f in uploads]
    token=data.get('submission_token')
    digest=hashlib.sha256(json.dumps({'fields':sorted((k,sorted(data.getlist(k)) if hasattr(data,'getlist') else data[k]) for k in data if k not in ('csrfmiddlewaretoken','document_search')),'files':[(f['name'],f['sha256']) for f in prepared]},default=str,ensure_ascii=False).encode()).hexdigest()
    if not pk and token:
        previous=CounterpartyAgreement.objects.filter(creation_token=token).first()
        if previous:
            if previous.created_by_id!=actor.pk:raise PermissionDenied
            if previous.creation_digest!=digest:raise ValidationError('Этот запрос уже сохранён с другими данными. Откройте карточку договора.')
            return previous
    row=get_object_or_404(CounterpartyAgreement.objects.select_for_update(),pk=pk) if pk else CounterpartyAgreement(pk=new_id or uuid.uuid4(),created_by=actor)
    before={k:str(getattr(row,k)) for k in FIELDS} if pk else {}
    form=AgreementForm(data,user=actor,instance=row)
    if not form.is_valid():raise ValidationError([str(e) for errors in form.errors.values() for e in errors])
    if form.cleaned_data['expected_version']!=(row.version if pk else 0):raise ValidationError('Карточка изменилась. Обновите страницу.')
    stored=[]
    try:
        with transaction.atomic():
            row=form.save(commit=False);row.version=row.version+1 if pk else 1
            contracts=list(form.cleaned_data['customer_contracts'].order_by('pk'));objects=list(form.cleaned_data['project_objects'].order_by('pk'))
            row.project_object=objects[0] if objects else None
            row.customer_contract=next((c for c in contracts if not row.project_object or c.objects_linked.filter(pk=row.project_object.pk).exists()),contracts[0] if contracts else None)
            if not pk:row.creation_token=token or None;row.creation_digest=digest
            row.save();form.save_m2m()
            for file in prepared:
                key=store_file(file);stored.append(key)
                doc=Document.objects.create(legal_entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID),created_by=actor,title=file['name'],section='contracts',kind='work',link_not_required=not bool(objects or contracts))
                doc.objects_linked.set(objects);doc.contracts_linked.set(contracts)
                make_version(doc,actor,file,key=key);row.documents.add(doc)
            audit(actor,'agreement_updated',agreement=str(row.pk),before=before,after={k:str(getattr(row,k)) for k in FIELDS},contracts=[str(c.pk) for c in contracts],objects=[str(o.pk) for o in objects],documents=[str(p) for p in row.documents.values_list('pk',flat=True)])
    except Exception:
        for key in stored:private_path(key).unlink(missing_ok=True)
        raise
    return row

@login_required
def home(request):
    guard(request.user);q=request.GET.get('q','').strip()[:200]
    rows=CounterpartyAgreement.objects.filter(archived=request.GET.get('archive')=='1').prefetch_related('customer_contracts','project_objects').order_by('-updated_at')
    if q:rows=rows.filter(Q(number__icontains=q)|Q(counterparty__icontains=q)|Q(subject__icontains=q))
    if request.GET.get('kind'):rows=rows.filter(kind=request.GET['kind'])
    for key,field in [('contract','customer_contracts'),('object','project_objects')]:
        if request.GET.get(key):
            try:rows=rows.filter(**{field+'__in':identifiers(request.GET,key)}).distinct()
            except ValueError:rows=rows.none()
    return render(request,'projects/agreements.html',{'page':Paginator(rows,30).get_page(request.GET.get('page')),'q':q,'kinds':CounterpartyAgreement.KINDS})

@login_required
def edit(request,pk=None):
    guard(request.user);row=get_object_or_404(CounterpartyAgreement,pk=pk) if pk else None
    initial={'expected_version':row.version if row else 0,'document_search':request.GET.get('document_search','')}
    form=AgreementForm(request.POST if request.method=='POST' else None,user=request.user,instance=row,initial=initial)
    if request.method=='POST' and request.POST.get('_search_documents')=='1':
        from django.forms.utils import ErrorDict
        form._errors=ErrorDict();form.cleaned_data={}
    if request.method=='POST' and request.POST.get('_search_documents')!='1' and form.is_valid():
        try:result=save(request.user,pk,request.POST,uploads=request.FILES.getlist('attachments'))
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('agreement_detail',pk=result.pk)
    return render(request,'projects/agreement_form.html',{'form':form,'row':row,'document_search':request.POST.get('document_search','') if request.method=='POST' else request.GET.get('document_search',''),'searching_documents':request.method=='POST' and request.POST.get('_search_documents')=='1'})

@login_required
def detail(request,pk):
    guard(request.user);row=get_object_or_404(CounterpartyAgreement,pk=pk)
    events=list(AuditEvent.objects.filter(action='agreement_updated',details__agreement=str(row.pk)).select_related('actor').order_by('-created_at')[:50])
    labels={field.name:str(field.verbose_name) for field in CounterpartyAgreement._meta.fields}
    for event in events:event.change_rows=[(labels.get(k,k),dict(CounterpartyAgreement.KINDS).get(v,v) if k=='kind' else (rub(v) if v not in ('None','') else 'Не указано') if k=='amount' else ('Да' if v=='True' else 'Нет') if k=='archived' else 'Не указано' if v=='None' else v) for k,v in event.details.get('after',{}).items()]
    return render(request,'projects/agreement_detail.html',{'row':row,'documents':row.documents.filter(pk__in=visible_documents(request.user)),'events':events[:50]})

class UploadForm(forms.Form):file=forms.FileField(label='Договоры XLSX')

def apply(user,rows):
    # Actor lock serializes receipts, edits and complete workbook application.
    actor=User.objects.select_for_update().get(pk=user.pk);guard(actor)
    for number,values in rows:
        data=dict(zip(FIELDS,values[2:2+len(FIELDS)]));data['expected_version']=values[1] or '0'
        data['archived']=data['archived']=='1'
        if values[2+FIELDS.index('archived')] not in ('','0','1'):raise ValidationError(f'Строка {number}: Архив — 0 или 1.')
        row=CounterpartyAgreement.objects.filter(pk=values[0]).first()
        for key,primary,index in [('customer_contracts','customer_contract',0),('project_objects','project_object',1)]:
            extra=values[2+len(FIELDS)+index] if len(values)>2+len(FIELDS)+index else None
            if extra is not None:data[key]=[v for v in [data[primary],*extra.split(';')] if v]
            elif row:data[key]=list(getattr(row,key).values_list('pk',flat=True))
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
                values.extend(';'.join(str(v) for v in getattr(r,key).exclude(pk=getattr(r,primary+'_id')).values_list('pk',flat=True)) for key,primary in [('customer_contracts','customer_contract'),('project_objects','project_object')]);rows.append(values)
        response=HttpResponse(human_xlsx.workbook(request.user,'agreements',rows,HEADERS+EXTRA_HEADERS),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');response['Content-Disposition']='attachment; filename="armada-counterparty-agreements.xlsx"';return response
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
                book=read_book(form.cleaned_data['file'])
                extended=any(str(v) in (EXTRA_HEADERS[0],human_xlsx.PREFIX+EXTRA_HEADERS[0],human_xlsx.label(EXTRA_HEADERS[0])) for (r,c),v in book['cells'].items() if r==1)
                book=human_xlsx.normalize(request.user,'agreements',book,HEADERS+EXTRA_HEADERS if extended else HEADERS);cells=book['cells']
                if book['formulas']:raise ValidationError('Замените формулы значениями.')
                if [str(cells.get((1,i+1),'')).strip() for i in range(len(HEADERS))]!=HEADERS:raise ValidationError('Используйте шаблон договоров с контрагентами.')
                seen=set()
                for n in sorted({r for r,c in cells if r>1}):
                    values=[str(cells.get((n,i+1),'')).strip() for i in range(len(HEADERS)+(len(EXTRA_HEADERS) if cells.get((1,len(HEADERS)+1))==EXTRA_HEADERS[0] else 0))]
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
