"""Versioned spreadsheet editing of objects, contract cards and personnel.

Access and account linking are deliberately handled by the existing explicit workflows.
A preview rehearses the same atomic operation that is repeated at commit time.
"""
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction, DatabaseError
from django.http import Http404, HttpResponse
from django.shortcuts import render, redirect
from accounts.models import User
from accounts.permissions import permitted
from accounts.views import audit
from documents.access import visible_people
from documents.forms import PersonForm
from documents.models import Person
from repairs.excel import workbook
from repairs.importer import read_book
from .access import visible_objects, visible_contracts
from .forms import ObjectForm, ContractForm
from .models import (LegalEntity, ProjectObject, Contract, ContractObject, ObjectAccess,
                     ContractAccess, RegistryImportReceipt, COMPANY_TAX_ID, SHARED_FIELDS,
                     STATUSES, WORK_KINDS)

TITLES={'objects':'Объекты','contracts':'Контракты','people':'Персонал'}
BACK={'objects':'unassigned_areas','contracts':'contracts','people':'people'}
OBJECT_FIELDS=['name','kind','location','status','start_date','end_date']
CONTRACT_FIELDS=['number','date','subject','work_kinds','counterparty','deadline','status']
PRIVATE_FIELDS=['amount','private_notes']
HEADERS={
    'objects':['ID объекта','Название','Вид объекта','Местоположение','Состояние','Начало работ','Окончание работ','Версия'],
    'contracts':['ID контракта','Номер договора','Дата договора','Предмет договора','Виды работ','Контрагент','Срок окончания','Состояние','ID объектов (через ;)','Версия'],
    'people':['ID сотрудника','ФИО','Должность','В архиве (справочно, 0/1)','Версия'],
}
PRIVATE_HEADERS=['Сумма, ₽','Внутренние примечания']
FIELD_FLAGS={'number':'show_number','date':'show_date','subject':'show_subject','counterparty':'show_counterparty','deadline':'show_deadline'}
SALT='registry-excel-v1'

def permission(kind):
    if kind not in TITLES:raise Http404
    return 'personnel.manage' if kind=='people' else 'projects.manage'

def require_registry(view):
    @login_required
    @wraps(view)
    def wrapped(request,kind):
        if not permitted(request.user,permission(kind)):raise PermissionDenied
        return view(request,kind)
    return wrapped

def headers(user,kind):
    return HEADERS[kind]+(PRIVATE_HEADERS if kind=='contracts' and user.is_leader else [])

def scalar(value):
    if value is None:return ''
    if isinstance(value,date):return value.isoformat()
    return str(value)

def export_rows(user,kind,archived=False):
    if not permitted(user,permission(kind)):raise PermissionDenied
    if kind=='objects':
        qs=visible_objects(user).order_by('name','pk')
        return [[str(o.pk),*[o.get_status_display() if f=='status' else scalar(getattr(o,f)) for f in OBJECT_FIELDS],o.version] for o in qs]
    if kind=='people':
        return [[str(p.pk),p.full_name,p.position,int(p.archived),p.revision] for p in visible_people(user).filter(archived=archived).order_by('full_name','pk')]
    qs=visible_contracts(user).prefetch_related('objects_linked','access_rules').order_by('pk')
    qs=qs.filter(status='archived') if archived else qs.exclude(status='archived')
    allowed_objects=set(visible_objects(user).values_list('pk',flat=True))
    rows=[]
    for contract in qs:
        rule=None if user.is_leader else next((r for r in contract.access_rules.all() if r.user_id==user.pk),None)
        fields=[]
        for field in CONTRACT_FIELDS:
            value=getattr(contract,field)
            if not user.is_leader and field in FIELD_FLAGS and (not rule or not getattr(rule,FIELD_FLAGS[field])):value=''
            fields.append(';'.join(dict(WORK_KINDS)[k] for k in value) if field=='work_kinds' else contract.get_status_display() if field=='status' else scalar(value))
        linked=';'.join(sorted(str(o.pk) for o in contract.objects_linked.all() if o.pk in allowed_objects))
        row=[str(contract.pk),*fields,linked,contract.version]
        if user.is_leader:row += [scalar(contract.amount),contract.private_notes]
        rows.append(row)
    return rows

@require_registry
def export(request,kind):
    rows=[] if request.GET.get('template')=='1' else export_rows(request.user,kind,request.GET.get('archive')=='1')
    response=HttpResponse(workbook(rows,headers=headers(request.user,kind),sheet_name=TITLES[kind]),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition']=f'attachment; filename="armada-{kind}.xlsx"'
    response['Cache-Control']='private, no-store'
    audit(request.user,'document_exported',kind='registry-'+kind,count=len(rows),format='xlsx')
    return response

class UploadForm(forms.Form):
    file=forms.FileField(label='Книга XLSX',widget=forms.FileInput(attrs={'accept':'.xlsx'}))

def normalize_date(raw,epoch):
    if not raw:return ''
    for fmt in ('%Y-%m-%d','%d.%m.%Y'):
        try:return datetime.strptime(raw,fmt).date().isoformat()
        except ValueError:pass
    try:
        number=Decimal(raw)
        if number.is_finite() and number==number.to_integral_value() and 0<number<200000:
            return (epoch+timedelta(days=int(number))).isoformat()
    except InvalidOperation:pass
    raise ValidationError('Дата: используйте ГГГГ-ММ-ДД, ДД.ММ.ГГГГ или дату Excel.')

def parse_rows(upload,kind,user):
    book=read_book(upload)
    if book['formulas']:raise ValidationError('Формулы не поддерживаются. Вставьте значения.')
    cells=book['cells'];expected=headers(user,kind)
    if [str(cells.get((1,i+1),'')).strip() for i in range(len(expected))]!=expected or any(c>len(expected) and str(v).strip() for (r,c),v in cells.items()):
        raise ValidationError('Столбцы не совпадают с шаблоном. Скачайте актуальный шаблон этого реестра.')
    rows=[];seen=set()
    for number in sorted({r for r,c in cells if r>1}):
        values=[str(cells.get((number,i+1),'')).strip() for i in range(len(expected))]
        if not any(values):continue
        if len(rows)>=500:raise ValidationError('За один импорт — до 500 строк. Разделите файл.')
        if any(len(v)>10000 for v in values):raise ValidationError(f'Строка {number}: слишком длинное значение.')
        try:
            if values[0]:
                values[0]=str(uuid.UUID(values[0]))
                if values[0] in seen:raise ValidationError('ID повторяется в файле.')
                seen.add(values[0])
            for i in ([5,6] if kind=='objects' else [2,6] if kind=='contracts' else []):
                values[i]=normalize_date(values[i],book['epoch'])
        except (ValueError,ValidationError) as exc:
            raise ValidationError(f'Строка {number}: '+(exc.messages[0] if isinstance(exc,ValidationError) else 'Проверьте ID.')) from exc
        rows.append({'row':number,'values':values})
    if not rows:raise ValidationError('В файле нет данных.')
    with transaction.atomic():
        apply_rows(user,kind,rows)
        transaction.set_rollback(True)
    return rows

def fresh_actor(user,kind):
    actor=User.objects.select_for_update(no_key=True).get(pk=user.pk)
    if actor.session_version!=user.session_version or not permitted(actor,permission(kind)):raise PermissionDenied
    return actor

def lock_rows(model,ids):
    # Avoid waiting in reverse order against existing UI operations/access changes.
    try:
        with transaction.atomic():
            list(model.objects.select_for_update(nowait=True).filter(pk__in=ids).order_by('pk'))
    except DatabaseError as exc:raise ValidationError('Записи сейчас изменяются. Повторите проверку файла.') from exc

def form_error(form):
    return '; '.join((str(form.fields[key].label)+': ' if key in form.fields else '')+', '.join(str(e) for e in errors) for key,errors in form.errors.items())

def apply_rows(user,kind,rows):
    """Atomic for direct callers too; receipt handled by the upload transaction."""
    with transaction.atomic():
        actor=fresh_actor(user,kind)
        try:ids={uuid.UUID(r['values'][0]) for r in rows if r['values'][0]}
        except (ValueError,KeyError,IndexError) as exc:raise ValidationError('Проверьте ID.') from exc
        if len(ids)!=sum(bool(r['values'][0]) for r in rows):raise ValidationError('ID повторяется в файле.')
        model={'objects':ProjectObject,'contracts':Contract,'people':Person}[kind]
        scope={'objects':visible_objects,'contracts':visible_contracts,'people':visible_people}[kind](actor)
        if ids-set(scope.filter(pk__in=ids).values_list('pk',flat=True)):
            raise ValidationError('Одна из записей недоступна. Проверьте ID и доступ.')
        lock_rows(model,ids)
        if kind=='contracts':
            object_ids=set()
            try:
                for row in rows:
                    object_ids.update(uuid.UUID(v.strip()) for v in row['values'][8].split(';') if v.strip())
            except (ValueError,IndexError) as exc:raise ValidationError('Проверьте ID объектов.') from exc
            if object_ids-set(visible_objects(actor).filter(pk__in=object_ids).values_list('pk',flat=True)):
                raise ValidationError('Проверьте ID объектов и доступ к ним.')
            lock_rows(ProjectObject,object_ids)
        for row in rows:
            try:_apply_row(actor,kind,row['values'])
            except (ValidationError,ValueError,TypeError) as exc:
                detail=exc.messages[0] if isinstance(exc,ValidationError) else 'Проверьте ID и целую версию записи.'
                raise ValidationError(f'Строка {row["row"]}: {detail}') from exc
        return actor

def _apply_row(actor,kind,v):
    if len(v)!=len(headers(actor,kind)):raise ValidationError('Состав столбцов изменился. Проверьте файл заново.')
    scope={'objects':visible_objects,'contracts':visible_contracts,'people':visible_people}[kind](actor)
    obj=scope.filter(pk=uuid.UUID(v[0])).first() if v[0] else None
    if v[0] and obj is None:raise ValidationError('Запись недоступна. Для новой строки очистите ID и версию.')
    version_index={'objects':7,'contracts':9,'people':4}[kind]
    version=getattr(obj,'revision' if kind=='people' else 'version',None)
    if obj and version!=int(v[version_index]):raise ValidationError('Запись изменилась. Выгрузите актуальный реестр.')
    if not obj and v[version_index]:raise ValidationError('Для новой строки оставьте ID и версию пустыми.')
    if kind=='objects':
        v=list(v);v[4]=dict((label,key) for key,label in STATUSES).get(v[4],v[4])
        before={f:scalar(getattr(obj,f)) for f in OBJECT_FIELDS} if obj else {}
        form=ObjectForm(dict(zip(OBJECT_FIELDS,v[1:7])),instance=obj)
    elif kind=='people':
        v=list(v)
        if not obj and not v[3]:v[3]='0'
        if v[3] not in ('0','1') or v[3]!=str(int(obj.archived if obj else False)):
            raise ValidationError('Архив не меняется через Excel. Используйте действие в карточке сотрудника.')
        before={f:getattr(obj,f) for f in ['full_name','position']} if obj else {}
        form=PersonForm({'full_name':v[1],'position':v[2]},instance=obj)
    else:
        if obj and obj.status=='archived':raise ValidationError('Контракт в архиве. Восстановление выполняет руководитель из карточки.')
        if obj and not actor.is_leader and obj.objects_linked.exclude(pk__in=visible_objects(actor)).exists():
            raise ValidationError('Для изменения контракта нужен доступ ко всем связанным объектам.')
        before={f:scalar(getattr(obj,f)) if f!='work_kinds' else list(obj.work_kinds) for f in CONTRACT_FIELDS+PRIVATE_FIELDS} if obj else {}
        before['objects']=sorted(str(i) for i in obj.objects_linked.values_list('pk',flat=True)) if obj else []
        data=dict(zip(CONTRACT_FIELDS,v[1:8]));data['rnu']=obj.rnu if obj else ''; data['work_kinds']=[dict((label,key) for key,label in WORK_KINDS).get(x.strip(),x.strip()) for x in v[4].split(';') if x.strip()]
        data['status']=dict((label,key) for key,label in STATUSES).get(data['status'],data['status'])
        data['linked_objects']=[str(uuid.UUID(x.strip())) for x in v[8].split(';') if x.strip()]
        if len(set(data['linked_objects']))!=len(data['linked_objects']):raise ValidationError('Объект повторяется в связях контракта.')
        if actor.is_leader:
            data.update(zip(PRIVATE_FIELDS,v[10:12]))
            data['amount']=data['amount'].replace(' ','').replace('\u00a0','').replace(',','.')
        form=ContractForm(data,instance=obj,user=actor)
        for field in FIELD_FLAGS:
            if field not in form.fields and data[field]:raise ValidationError('В недоступных реквизитах должны оставаться пустые ячейки.')
    if not form.is_valid():raise ValidationError(form_error(form))
    saved=form.save(commit=False)
    if kind=='contracts':
        desired=set(o.pk for o in form.cleaned_data['linked_objects'])
        from .forms import validate_new_area_links
        validate_new_area_links(obj,desired)
        from repairs.models import Job
        if obj and (Job.objects.filter(contract=obj).exclude(section__project_object_id__in=desired).exists() or (Job.objects.filter(contract=obj).exists() and 'repairs' not in saved.work_kinds)):
            raise ValidationError('Нельзя убрать объект или вид работ, пока с ними связаны ремонтные задания.')
    if kind=='contracts' and obj:
        for related in (obj.brigade_assignments,obj.equipment_placements,obj.subcontract_orders,obj.daily_works):
            if related.exclude(project_object_id__in=desired).exists():
                raise ValidationError('Нельзя убрать объект с назначениями бригад, техники или поручениями субподрядчикам.')
        if obj.daily_works.exclude(kind__in=saved.work_kinds).exists():raise ValidationError('Нельзя убрать вид работ с ежедневными выполнениями.')
        if obj.subcontract_orders.exclude(kind__in=saved.work_kinds).exists():
            raise ValidationError('Нельзя убрать вид работ, по которому есть поручения субподрядчику.')
    if not obj:
        saved.legal_entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
        if kind=='people':saved.created_by=actor
    setattr(saved,'revision' if kind=='people' else 'version',(version+1) if obj else 1)
    saved.save()
    if kind=='objects' and not obj and not actor.is_leader:ObjectAccess.objects.create(user=actor,project_object=saved,granted_by=actor)
    if kind=='contracts':
        previous=set(saved.objects_linked.values_list('pk',flat=True))
        ContractObject.objects.filter(contract=saved).exclude(project_object_id__in=desired).delete()
        for oid in sorted(desired-previous,key=str):ContractObject.objects.create(contract=saved,project_object_id=oid)
        for rule in saved.access_rules.select_related('user'):
            if not ObjectAccess.objects.filter(user=rule.user,project_object__contracts=saved).exists() and not (not desired and permitted(rule.user,'projects.manage')):rule.delete()
        if not obj and not actor.is_leader:ContractAccess.objects.create(user=actor,contract=saved,granted_by=actor,**{f:True for f in SHARED_FIELDS})
    action='person_updated' if kind=='people' else ('object_updated' if obj else 'object_created') if kind=='objects' else ('contract_updated' if obj else 'contract_created')
    after={f:scalar(getattr(saved,f)) for f in (OBJECT_FIELDS if kind=='objects' else ['full_name','position'] if kind=='people' else CONTRACT_FIELDS+PRIVATE_FIELDS)}
    if kind=='contracts':after['objects']=sorted(str(i) for i in desired)
    audit(actor,action,entity_id=str(saved.pk),source='excel',before=before,after=after)

def commit_preview(user,kind,payload):
    if payload['actor']!=str(user.pk) or payload['kind']!=kind or payload['session']!=user.session_version:raise PermissionDenied
    with transaction.atomic():
        actor=fresh_actor(user,kind)
        receipt=RegistryImportReceipt.objects.filter(token=payload['token']).first()
        if receipt and (receipt.actor_id!=actor.pk or receipt.kind!=kind):raise PermissionDenied
        if not receipt:
            apply_rows(actor,kind,payload['rows'])
            RegistryImportReceipt.objects.create(token=payload['token'],actor=actor,kind=kind)

@require_registry
def upload(request,kind):
    form=UploadForm(request.POST or None,request.FILES or None);rows=None;preview='';status=200
    if request.method=='POST':
        if request.POST.get('apply')=='1':
            try:
                payload=signing.loads(request.POST.get('preview',''),salt=SALT,max_age=3600)
                commit_preview(request.user,kind,payload)
            except (signing.BadSignature,ValidationError,ValueError,KeyError) as exc:
                form.add_error(None,exc if isinstance(exc,ValidationError) else 'Предварительный просмотр устарел. Проверьте файл заново.');status=409
            else:
                messages.success(request,'Проверенные строки Excel сохранены. Прежние значения записаны в историю.');return redirect(BACK[kind])
        elif form.is_valid():
            try:
                rows=parse_rows(form.cleaned_data['file'],kind,request.user)
                preview=signing.dumps({'actor':str(request.user.pk),'kind':kind,'session':request.user.session_version,'rows':rows,'token':str(uuid.uuid4())},salt=SALT,compress=True)
                if len(preview)>96*1024:raise ValidationError('Слишком большой объём сведений для одного переноса. Разделите файл на части.')
            except ValidationError as exc:
                rows=None;preview='';form.add_error('file',exc)
    return render(request,'projects/registry_excel.html',{'form':form,'rows':rows,'preview':preview,'headers':headers(request.user,kind),'kind':kind,'title':TITLES[kind],'back':BACK[kind],'statuses':STATUSES,'work_kinds':WORK_KINDS},status=status)
