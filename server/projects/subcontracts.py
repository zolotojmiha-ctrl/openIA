"""Explicit company membership and per-order access never grant parent cards or files."""
import hashlib,json,re
from decimal import Decimal
from django.db import transaction
from django.db.models import Q
from django.core.exceptions import ValidationError,PermissionDenied
from django.utils import timezone
from accounts.models import User
from accounts.permissions import permitted
from accounts.views import audit
from documents.access import visible_documents
from documents.storage import store_file,private_path
from .models import Subcontractor,SubcontractRepresentative,SubcontractOrder,SubcontractOrderAccess,SubcontractDocument,SubcontractResult,SubcontractAttachment,SubcontractDecision,LegalEntity,COMPANY_TAX_ID
from .access import visible_objects,visible_contracts

def own_order_filter(user,report=False):
    q=Q(access_rules__active=True,access_rules__representative__user=user,access_rules__representative__active=True,company__archived=False,archived=False)
    return q & Q(access_rules__can_report=True) if report else q

def orders(user,include_archived=False):
    qs=SubcontractOrder.objects.filter(company__legal_entity__tax_id=COMPANY_TAX_ID,contract__in=visible_contracts(user),project_object__in=visible_objects(user))
    if not user.is_authenticated or not user.is_active:return qs.none()
    if not include_archived:qs=qs.filter(archived=False,company__archived=False)
    if user.role=='subcontractor':qs=qs.filter(pk__in=SubcontractOrder.objects.filter(own_order_filter(user)).values('pk'))
    elif not permitted(user,'subcontract.manage'):qs=qs.filter(responsible=user)
    return qs.select_related('company','project_object','contract','responsible')

def companies(user):
    qs=Subcontractor.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID)
    if not permitted(user,'subcontract.manage'):qs=qs.filter(pk__in=orders(user).values('company_id'))
    return qs

def can_report(user,order):
    if not user.is_active or order.archived or order.company.archived or order.contract.status=='archived' or order.project_object.status=='archived':return False
    if not orders(user).filter(pk=order.pk).exists():return False
    if user.is_leader or permitted(user,'subcontract.manage'):return True
    if user.role=='subcontractor':return SubcontractOrder.objects.filter(pk=order.pk).filter(own_order_filter(user,report=True)).exists()
    return order.responsible_id==user.pk

def manager(user):
    actor=User.objects.select_for_update(no_key=True).get(pk=user.pk)
    if not permitted(actor,'subcontract.manage') or actor.session_version!=user.session_version:raise PermissionDenied
    return actor

@transaction.atomic
def save_company(user,pk,version,data):
    actor=manager(user);entity=LegalEntity.objects.select_for_update().get(tax_id=COMPANY_TAX_ID)
    row=companies(actor).select_for_update().get(pk=pk) if pk else Subcontractor(legal_entity=entity,created_by=actor)
    if pk and row.version!=version:raise ValidationError('Компания изменена. Обновите страницу.')
    if not re.fullmatch(r'(?:[0-9]{10}|[0-9]{12})',data['tax_id']):raise ValidationError('ИНН: 10 или 12 цифр.')
    if pk and row.tax_id!=data['tax_id'] and row.orders.exists():raise ValidationError('У компании уже есть поручения. Для другого ИНН создайте другую компанию.')
    if data.get('archived') and row.orders.filter(archived=False).exists():raise ValidationError('Сначала завершите и архивируйте поручения компании.')
    before={k:getattr(row,k) for k in ('name','tax_id','contact','notes','archived')} if pk else None
    for k in ('name','tax_id','contact','notes','archived'):setattr(row,k,data.get(k,False if k=='archived' else ''))
    row.full_clean()
    if pk:row.version+=1
    row.save();audit(actor,'subcontract_updated',kind='company',id=str(row.pk),previous=before,name=row.name,tax_id=row.tax_id,archived=row.archived)
    return row

@transaction.atomic
def save_representative(user,company_id,pk,version,account,active):
    actor=manager(user);company=companies(actor).select_for_update().get(pk=company_id)
    if company.archived and active:raise ValidationError('Компания в архиве.')
    target=User.objects.get(pk=account.pk)
    if (active or not pk) and (target.is_leader or target.role!='subcontractor' or not target.is_active):raise ValidationError('Выберите действующую учётную запись представителя субподрядчика.')
    row=SubcontractRepresentative.objects.select_for_update().get(pk=pk,company=company) if pk else SubcontractRepresentative(company=company,user=target,created_by=actor)
    if pk and (row.version!=version or row.user_id!=target.pk):raise ValidationError('Представитель изменён. Учётная запись существующей связи не переписывается.')
    row.active=active;row.full_clean()
    if pk:row.version+=1
    row.save();audit(actor,'subcontract_access_changed',kind='membership',company=str(company.pk),representative=row.pk,user=str(target.pk),active=active)
    return row

ORDER_FIELDS=('title','scope','planned_quantity','unit','responsible','archived')
@transaction.atomic
def save_order(user,pk,version,data):
    actor=manager(user);company=companies(actor).select_for_update().get(pk=data['company'].pk)
    row=orders(actor,True).select_for_update(no_key=True,of=('self',)).get(pk=pk) if pk else SubcontractOrder(created_by=actor)
    if pk and row.version!=version:raise ValidationError('Поручение изменено. Обновите страницу.')
    contract=data['contract'];obj=data['project_object']
    if company.archived or not visible_contracts(actor).filter(pk=contract.pk,objects_linked=obj).exclude(status='archived').exists() or not visible_objects(actor).filter(pk=obj.pk).exclude(status='archived').exists():raise ValidationError('Компания, контракт или объект недоступны / в архиве; проверьте связь объекта с контрактом.')
    if data['kind'] not in contract.work_kinds:raise ValidationError('Этот вид работ не выбран в контракте.')
    if pk and (row.results.exists() or row.repair_jobs.exists() or row.access_rules.exists() or row.file_links.exists()) and (row.company_id!=company.pk or row.contract_id!=contract.pk or row.project_object_id!=obj.pk or row.kind!=data['kind']):raise ValidationError('Поручение с историей не переносится к другой компании, объекту или виду работ. Создайте новое.')
    responsible=data.get('responsible')
    if responsible and (not responsible.is_active or responsible.role=='subcontractor' or not responsible.is_leader and (not responsible.object_assignments.filter(project_object=obj).exists() or not responsible.contract_access.filter(contract=contract).exists())):raise ValidationError('Ответственному нужен доступ к объекту и контракту.')
    if data.get('planned_quantity') is not None and (data['planned_quantity']<0 or not data.get('unit')):raise ValidationError('Плановое количество неотрицательное; укажите единицу.')
    if data.get('archived') and (row.state=='reported' or row.repair_jobs.filter(defects__archived_at__isnull=True).exclude(state='confirmed').exists() or row.results.exists() and row.state!='confirmed'):raise ValidationError('Сначала проверьте выполнение и завершите работы.')
    if pk and row.results.filter(quantity__isnull=False).exists() and row.unit!=data.get('unit',''):raise ValidationError('Единицу поручения с результатами не меняют. Создайте отдельное поручение.')
    before={'plan':str(row.planned_quantity) if row.planned_quantity is not None else '','unit':row.unit,'scope':row.scope,'responsible':str(row.responsible_id or ''),'company':str(row.company_id or ''),'contract':str(row.contract_id or ''),'object':str(row.project_object_id or ''),'title':row.title,'archived':row.archived} if pk else None
    row.company=company;row.contract=contract;row.project_object=obj;row.kind=data['kind']
    for key in ORDER_FIELDS:setattr(row,key,data.get(key,False if key=='archived' else None if key in ('planned_quantity','responsible') else ''))
    row.full_clean()
    if pk:row.version+=1
    row.save();audit(actor,'subcontract_updated',kind='order',id=str(row.pk),previous=before,company=str(company.pk),contract=str(contract.pk),object=str(obj.pk),title=row.title,scope=row.scope,planned_quantity=str(row.planned_quantity) if row.planned_quantity is not None else '',unit=row.unit,responsible=str(row.responsible_id or ''),archived=row.archived)
    return row

@transaction.atomic
def grant_order(user,order_id,representative_id,active,report,version=None):
    actor=manager(user);order=orders(actor,True).select_for_update(no_key=True,of=('self',)).get(pk=order_id)
    representative=SubcontractRepresentative.objects.select_for_update().get(pk=representative_id,company=order.company)
    if active and (order.archived or order.company.archived or not representative.active or not representative.user.is_active):raise ValidationError('Поручение или представитель недействительны.')
    if active and (not visible_objects(representative.user).filter(pk=order.project_object_id).exists() or not visible_contracts(representative.user).filter(pk=order.contract_id).exists()):raise ValidationError('Сначала отдельно выдайте представителю доступ к объекту и контракту. Права на файлы выдаются в карточке документа.')
    row=SubcontractOrderAccess.objects.filter(order=order,representative=representative).first()
    if row and row.version!=version:raise ValidationError('Доступ уже изменён. Обновите страницу.')
    if not row:row=SubcontractOrderAccess(order=order,representative=representative,granted_by=actor)
    else:row.version+=1
    row.active=active;row.can_report=bool(report and active);row.save();audit(actor,'subcontract_access_changed',kind='order',order=str(order.pk),representative=representative.pk,active=active,can_report=row.can_report)
    return row

@transaction.atomic
def link_repairs(user,order_id,versions):
    from repairs.access import can_prepare,visible_jobs
    from repairs.models import Report
    actor=manager(user)
    if not can_prepare(actor):raise PermissionDenied
    order=orders(actor).select_for_update(no_key=True,of=('self',)).get(pk=order_id)
    if order.kind!='repairs':raise ValidationError('Для дефектов нужен соответствующий вид поручения.')
    jobs=list(visible_jobs(actor).select_for_update(no_key=True,of=('self',)).filter(pk__in=versions).order_by('pk'))
    if not jobs or len(jobs)!=len(versions):raise ValidationError('Выбор работ недоступен.')
    for j in jobs:
        if j.contract_id!=order.contract_id or j.section.project_object_id!=order.project_object_id or j.state=='confirmed' or j.version!=versions[str(j.pk)] or j.subcontract_order_id and j.subcontract_order_id!=order.pk:raise ValidationError('Одна из работ изменилась, завершена или уже поручена другой компании.')
    for j in jobs:
        if j.subcontract_order_id==order.pk:continue
        j.subcontract_order=order;j.version+=1;j.save(update_fields=['subcontract_order','version'])
        Report.objects.create(job=j,author=actor,kind='assignment',performed_date=timezone.localdate(),note='Работа поручена субподрядчику: '+order.company.name,context={'subcontract_order':str(order.pk),'company':str(order.company_id)})
    audit(actor,'subcontract_updated',kind='repair_links',order=str(order.pk),jobs=[str(j.pk) for j in jobs])

def files(user):
    qs=SubcontractDocument.objects.filter(company__in=companies(user),document__in=visible_documents(user)).filter(Q(order__isnull=True)|Q(order__in=orders(user,True))).select_related('document','order')
    return qs if permitted(user,'subcontract.manage') else qs.filter(active=True)

@transaction.atomic
def save_document(user,company_id,pk,version,data):
    actor=manager(user);company=companies(actor).select_for_update().get(pk=company_id)
    document=data['document'];order=data.get('order')
    if not visible_documents(actor).filter(pk=document.pk).exists() or order and not orders(actor,True).filter(pk=order.pk,company=company).exists():raise ValidationError('Документ или поручение недоступны.')
    row=files(actor).select_for_update(of=('self',)).get(pk=pk,company=company) if pk else SubcontractDocument(company=company,created_by=actor)
    if pk and (row.version!=version or row.document_id!=document.pk or row.order_id!=(order.pk if order else None)):raise ValidationError('Связь изменилась. Другой файл / поручение добавляются новой связью.')
    row.document=document;row.order=order;row.label=data['label'];row.active=data['active'];row.full_clean()
    if pk:row.version+=1
    row.save();audit(actor,'subcontract_document_linked',company=str(company.pk),link=str(row.pk),document=str(document.pk),order=str(order.pk) if order else None,active=row.active,label=row.label)
    return row

def submit_result(user,order_id,data,accept=False):
    stored=[]
    try:
        with transaction.atomic():
            actor=User.objects.select_for_update(no_key=True).get(pk=user.pk)
            if not actor.is_active or actor.session_version!=user.session_version:raise PermissionDenied
            order=orders(actor).select_for_update(no_key=True,of=('self',)).filter(pk=order_id).first()
            if not order:raise PermissionDenied
            if actor.role=='subcontractor':lock_external(actor,order.pk,True)
            if not can_report(actor,order):raise PermissionDenied
            if order.kind=='repairs':raise ValidationError('По дефектам результат вносится в каждый отдельный ремонт.')
            payload={k:str(data.get(k) if data.get(k) is not None else '') for k in ('outcome','quantity','date','note')};payload['files']=[{'name':f['name'],'sha256':f['sha256']} for f in data.get('files',[])];payload['accept']=accept
            digest=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
            old=SubcontractResult.objects.filter(token=data['token']).first()
            if old:
                if old.order_id==order.pk and old.author_id==actor.pk and old.digest==digest:return old
                raise ValidationError('Запрос уже сохранён с другими сведениями.')
            if order.version!=data['expected_version']:raise ValidationError('Поручение изменено. Проверьте актуальную карточку.')
            if order.state in ('confirmed','reported') and not accept:raise ValidationError('Результат уже на проверке или подтверждён. Для исправления руководитель использует «Исправить и принять».')
            if accept and (not actor.is_leader or order.state not in ('confirmed','reported')):raise PermissionDenied
            if data['outcome'] not in ('complete','partial','problem') or not data.get('note','').strip() or len(data['note'])>3000:raise ValidationError('Выберите результат и укажите пояснение до 3000 символов.')
            if not data.get('date') or data['date']>timezone.localdate():raise ValidationError('Укажите фактическую дату, не в будущем.')
            q=data.get('quantity')
            if q is not None and (not Decimal(q).is_finite() or q<0 or q>=Decimal('1000000000') or Decimal(q).normalize().as_tuple().exponent < -3):raise ValidationError('Проверьте выполненное количество.')
            if q is not None and not order.unit:raise ValidationError('Для количества укажите единицу в поручении.')
            if order.planned_quantity is not None and data['outcome'] in ('complete','partial') and q is None:raise ValidationError('Укажите выполненное количество с начала работ.')
            if accept and data['outcome']!='complete':raise ValidationError('Принять можно завершённый результат.')
            result=SubcontractResult.objects.create(order=order,token=data['token'],digest=digest,outcome=data['outcome'],quantity=q,date=data['date'],note=data['note'],author=actor,correction=accept)
            for file in data.get('files',[]):
                key=store_file(file);stored.append(key);SubcontractAttachment.objects.create(result=result,storage_key=key,**{k:file[k] for k in ('name','content_type','size','sha256')})
            order.state='reported' if data['outcome']=='complete' else 'problem' if data['outcome']=='problem' else 'progress';order.version+=1
            if accept:SubcontractDecision.objects.create(result=result,actor=actor,decision='confirmed');order.state='confirmed'
            order.save(update_fields=['state','version']);audit(actor,'subcontract_reported',order=str(order.pk),result=str(result.pk),correction=accept,outcome=data['outcome'],quantity=str(q) if q is not None else '')
            return result
    except Exception:
        for key in stored:private_path(key).unlink(missing_ok=True)
        raise

@transaction.atomic
def decide(user,order_id,version,decision,note=''):
    actor=User.objects.select_for_update(no_key=True).get(pk=user.pk)
    if not actor.is_active or not actor.is_leader or actor.session_version!=user.session_version:raise PermissionDenied
    order=orders(actor).select_for_update(no_key=True,of=('self',)).get(pk=order_id);result=order.results.first()
    if order.version!=version or order.state!='reported' or not result or result.outcome!='complete' or hasattr(result,'decision'):raise ValidationError('Результат уже изменён или проверен.')
    if decision not in ('confirmed','revision') or decision=='revision' and not note.strip() or len(note)>3000:raise ValidationError('Для возврата укажите, что нужно исправить.')
    SubcontractDecision.objects.create(result=result,actor=actor,decision=decision,note=note)
    order.state='confirmed' if decision=='confirmed' else 'progress';order.version+=1;order.save(update_fields=['state','version'])
    audit(actor,'subcontract_decided',order=str(order.pk),result=str(result.pk),decision=decision,note=note)
    return result


def lock_external(user,order_id,report=False):
    order=orders(user).select_for_update(no_key=True,of=('self',)).filter(pk=order_id).first()
    if not order:raise PermissionDenied
    rep=SubcontractRepresentative.objects.select_for_update().filter(company=order.company,user=user).first()
    grant=SubcontractOrderAccess.objects.select_for_update().filter(order=order,representative=rep,active=True).first() if rep else None
    from .models import ObjectAccess,ContractAccess
    if not ObjectAccess.objects.select_for_update().filter(user=user,project_object=order.project_object).exists() or not ContractAccess.objects.select_for_update().filter(user=user,contract=order.contract).exists():raise PermissionDenied
    if not rep or not rep.active or not grant or report and not grant.can_report:raise PermissionDenied
    return order


@transaction.atomic
def unlink_repair(user,order_id,job_id,version,note):
    from repairs.access import can_prepare,visible_jobs
    from repairs.models import Report
    actor=manager(user)
    if not can_prepare(actor):raise PermissionDenied
    order=orders(actor,True).select_for_update(no_key=True,of=('self',)).get(pk=order_id)
    job=visible_jobs(actor).select_for_update(no_key=True,of=('self',)).get(pk=job_id,subcontract_order=order)
    if job.version!=version or job.state=='confirmed' or not note.strip() or len(note)>1500:raise ValidationError('Укажите причину до 1500 символов; подтверждённую или изменённую работу нельзя снять этим действием.')
    if job.state!='planned' and not actor.is_leader:raise PermissionDenied
    job.subcontract_order=None;job.version+=1;job.save(update_fields=['subcontract_order','version'])
    Report.objects.create(job=job,author=actor,kind='assignment',performed_date=timezone.localdate(),note='Снято поручение субподрядчику: '+note,context={'from_subcontract_order':str(order.pk),'company':str(order.company_id)})
    audit(actor,'subcontract_updated',kind='repair_unlinked',job=str(job.pk),order=str(order.pk),reason=note)


def returned_orders(user):
    from django.db.models import OuterRef,Subquery,F
    latest=SubcontractResult.objects.filter(order_id=OuterRef('pk')).order_by('-created_at','-pk').values('pk')[:1]
    return orders(user).filter(state='progress').annotate(_latest_result=Subquery(latest)).filter(results__pk=F('_latest_result'),results__decision__decision='revision')
