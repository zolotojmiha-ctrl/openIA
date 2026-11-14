"""Bounded, reviewed historical inserts into a separately pinned contract.

No existing jobs, stock items, staff grants or assignments are updated. ImportBatch
is only preview metadata; all actual defects/jobs use the ordinary repair models.
"""
import hashlib,json,uuid,re,math
from datetime import date,timedelta
from decimal import Decimal,InvalidOperation
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from accounts.models import User
from accounts.views import audit
from projects.models import Contract,ContractClosureEvent,COMPANY_TAX_ID
from projects.access import visible_contracts
from projects.archive import lifecycle_write
from documents.storage import validate_upload,store_file,private_path
from .models import Job,Defect,JobDefect,Report,Section,ImportBatch,METHODS
from .values import kilometre,identifier

TITLE='Архив устранённых дефектов МН Омск — Иркутск, 2024–2025'
MARKER='historical_archive_v1'
MAX_BYTES=3*1024*1024


def allowed(user):return user.is_authenticated and user.is_active and user.is_leader

def actor(user,lock=False):
    qs=User.objects.select_for_update(no_key=True) if lock else User.objects.all()
    fresh=qs.get(pk=user.pk)
    if not allowed(fresh) or fresh.session_version!=user.session_version:raise PermissionDenied
    return fresh

def norm(value):return re.sub(r'[^а-яa-z0-9]','',str(value).lower().replace('ё','е'))
def digest(jobs):return hashlib.sha256(json.dumps(jobs,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def scalar(model,name,value):return model._meta.get_field(name).clean(value,None)

def object_pairs(pairs):
    data={}
    for key,value in pairs:
        if key in data:raise ValueError
        data[key]=value
    return data

def read(upload):
    raw=upload.read(MAX_BYTES+1);upload.seek(0)
    if not raw or len(raw)>MAX_BYTES:raise ValidationError('Нужен подготовленный JSON-план размером до 3 МБ.')
    try:
        plan=json.loads(raw.decode('utf-8-sig'),object_pairs_hook=object_pairs)
        if not isinstance(plan,dict) or plan.get('format')!='armada-private-archive-plan-v1':raise ValueError
        target=plan['target_contract'];uuid.UUID(target['id'])
        if target['expected_number']!=TITLE:raise ValueError
        jobs=plan['jobs']
        if not isinstance(jobs,list) or not 1<=len(jobs)<=500 or plan.get('payload_sha256')!=digest(jobs):raise ValueError
        # Restrict nested metadata too, before persisting an opaque JSON source.
        def bounded(value,depth=0):
            if depth>14:raise ValueError
            if isinstance(value,dict):
                if len(value)>150:raise ValueError
                for k,v in value.items():
                    if not isinstance(k,str) or len(k)>200:raise ValueError
                    bounded(v,depth+1)
            elif isinstance(value,list):
                if len(value)>1000:raise ValueError
                for v in value:bounded(v,depth+1)
            elif isinstance(value,str) and len(value)>10000:raise ValueError
            elif isinstance(value,float) and not math.isfinite(value):raise ValueError
        bounded(plan)
    except (ValueError,KeyError,TypeError,RecursionError,UnicodeError):
        raise ValidationError('Не удалось проверить формат или контрольную сумму плана архива.') from None
    return plan,raw

def resolve(contract,jobs):
    areas=list(contract.objects_linked.all());bindings={}
    for row in jobs:
        name=row.get('project_object_name')
        if not isinstance(name,str) or len(name)>200:raise ValidationError('Не указан участок работы.')
        found=[area for area in areas if norm(area.name)==norm(name)]
        if len(found)!=1:raise ValidationError('Участок плана должен однозначно входить в архивный договор: '+name)
        sections=list(Section.objects.filter(project_object=found[0]).order_by('pk'))
        if len(sections)!=1:raise ValidationError('Для участка нужно однозначно выбрать существующий раздел нефтепровода: '+name)
        bindings[name]=str(sections[0].pk)
    return bindings

def normalized(plan,contract):
    if str(contract.pk)!=str(plan['target_contract']['id']) or contract.number!=TITLE:
        raise ValidationError('План предназначен для другого договора. Действующий договор не изменён.')
    jobs=[];seen=set();keys=set()
    try:
        for i,row in enumerate(plan['jobs'],1):
            if str(row['contract_id'])!=str(contract.pk):raise ValueError
            fields=row['job_fields'];src=fields['source']
            if not isinstance(src,dict) or src.get('historical_import') is not True or src.get('period')!='2024–2025':raise ValueError
            if fields.get('state')!='confirmed' or fields.get('assigned_to') is not None or fields.get('crew_assignment') is not None:raise ValueError
            actual=row['historical_result'];day=actual.get('performed_date')
            if day is not None:
                day=date.fromisoformat(day)
                if not 2024<=day.year<=2025:raise ValueError
            if src.get('completion_date')!=(day.isoformat() if day else None):raise ValueError
            clean={k:scalar(Job,k,fields.get(k)) for k in ('method','length_m','wall_mm','composition','notes')}
            if clean['length_m'] is not None and clean['length_m']<=0:raise ValueError
            parts=src.get('component_snapshots',[])
            if not isinstance(parts,list) or len(parts)>100:raise ValueError
            for part in parts:
                if part.get('method') not in set(dict(METHODS))|set(dict(METHODS).values()):raise ValueError
                mounted=date.fromisoformat(part['mount_date'])
                if not 2024<=mounted.year<=2025:raise ValueError
                size=part.get('length_mm')
                if size is not None and (not Decimal(str(size)).is_finite() or not 0<Decimal(str(size))<100000):raise ValueError
            defects=[]
            if not isinstance(row['defects'],list) or not 1<=len(row['defects'])<=50:raise ValueError
            for d in row['defects']:
                km=kilometre(d['kilometre']);number=identifier(d['number'],'№ дефекта');pipe=identifier(d.get('pipe_section',''),'№ секции',optional=True)
                identity=(norm(row['project_object_name']),str(km),number)
                if identity in seen:raise ValueError
                seen.add(identity);defects.append({'kilometre':str(km),'number':number,'pipe_section':pipe})
            # The importer owns these flags; supplied metadata cannot grant access,
            # fabricate coating consumption, override a target or write other models.
            source=dict(src,historical_import=True,completion_date=day.isoformat() if day else None,
                actual_coating_length_m=None,archive_payload_sha256=plan['payload_sha256'])
            key=hashlib.sha256(json.dumps([row['project_object_name'],defects],sort_keys=True).encode()).hexdigest()
            if key in keys:raise ValueError
            keys.add(key);source['archive_import_key']=key
            # Only JSON-native representations are stored in the preview.
            clean={k:str(v) if isinstance(v,Decimal) else v for k,v in clean.items()}
            jobs.append({'object':row['project_object_name'],'fields':clean,'source':source,'defects':defects,
                         'date':day.isoformat() if day else None})
        if len(seen)>1000:raise ValueError
    except (ValueError,KeyError,TypeError,InvalidOperation,ValidationError):
        raise ValidationError(f'Проверьте поля исторической работы {i}: даты, метод, длины и номера. Ничего не перенесено.') from None
    return jobs

def counts(payload):
    return {'jobs':len(payload['jobs']),'defects':sum(len(r['defects']) for r in payload['jobs']),
            'undated':sum(not r['date'] for r in payload['jobs'])}

def summary(batch):
    n=counts(batch.payload)
    return f'{batch.contract.number}\nРабот: {n["jobs"]}; дефектов: {n["defects"]}; работ без известной даты: {n["undated"]}.\nТекущие работы, склады и права не меняются. Повтор не создаёт дублей.'

def batch_for(user,pk):
    actor(user)
    batch=ImportBatch.objects.select_related('contract','section').filter(pk=pk,author=user,contract__in=visible_contracts(user)).first()
    if not batch or batch.payload.get('type')!=MARKER:raise PermissionDenied
    return batch

def prepare(user,contract_id,upload,stored_keys=None):
    plan,raw=read(upload);saved=None
    try:
        with transaction.atomic():
            fresh=actor(user,True)
            contract=Contract.objects.select_for_update(no_key=True).get(pk=contract_id,legal_entity__tax_id=COMPANY_TAX_ID)
            jobs=normalized(plan,contract)
            existing=ImportBatch.objects.filter(contract=contract,payload__type=MARKER,payload__digest=plan['payload_sha256'],applied_at__isnull=False).order_by('created_at').first()
            if existing:
                if existing.author_id!=fresh.pk:raise ValidationError('Этот план уже перенесён другим руководителем. Откройте архив договора.')
                return existing
            if contract.status!='planned':raise ValidationError('Для первого переноса архивный договор должен иметь статус «Подготовка».')
            if Job.objects.filter(contract=contract).exists() or Defect.objects.filter(contract=contract).exists():raise ValidationError('В выбранном договоре уже есть записи. Автоматическая перезапись запрещена.')
            bindings=resolve(contract,plan['jobs'])
            payload={'type':MARKER,'digest':plan['payload_sha256'],'contract_version':contract.version,'bindings':bindings,'jobs':jobs,'cancelled':False}
            data=validate_upload(SimpleUploadedFile(upload.name,raw));saved=store_file(data)
            if stored_keys is not None:stored_keys.append(saved)
            batch=ImportBatch.objects.create(contract=contract,section_id=next(iter(bindings.values())),author=fresh,
                filename=data['name'],storage_key=saved,size=data['size'],sha256=data['sha256'],sheet='Архив 2024–2025',payload=payload)
            return batch
    except Exception:
        if saved:private_path(saved).unlink(missing_ok=True)
        raise

@transaction.atomic
def apply(user,pk,expected_digest):
    fresh=actor(user,True)
    candidate=batch_for(fresh,pk)
    contract=Contract.objects.select_for_update(no_key=True).get(pk=candidate.contract_id)
    batch=ImportBatch.objects.select_for_update(of=('self',)).get(pk=pk)
    p=batch.payload
    if p.get('digest')!=expected_digest or p.get('cancelled'):raise ValidationError('Предпросмотр отменён или изменился.')
    if contract.number!=TITLE:raise ValidationError('Название архивного договора изменилось. Повторите предпросмотр.')
    if batch.applied_at:return counts(p)
    existing=list(Job.objects.filter(contract=contract).order_by('pk'))
    if existing:
        if (len(existing)==len(p['jobs']) and {j.source.get('archive_payload_sha256') for j in existing}=={p['digest']}
            and {j.source.get('archive_import_key') for j in existing}=={j['source']['archive_import_key'] for j in p['jobs']}
            and Defect.objects.filter(contract=contract).count()==counts(p)['defects']):return counts(p)
        raise ValidationError('В договоре появились записи. Ничего не заменено — нужен новый предпросмотр.')
    if contract.status!='planned' or contract.version!=p['contract_version'] or batch.created_at<timezone.now()-timedelta(hours=24):
        raise ValidationError('Договор изменился или предпросмотр устарел. Загрузите план заново.')
    if resolve(contract,[{'project_object_name':name} for name in p['bindings']])!=p['bindings']:
        raise ValidationError('Связь участка изменилась. Повторите предпросмотр.')
    section_objects={str(s.pk):s for s in Section.objects.filter(pk__in=p['bindings'].values())}
    for row in p['jobs']:
        source=row['source'];fields=row['fields']
        job=Job.objects.create(contract=contract,section=section_objects[p['bindings'][row['object']]],created_by=fresh,
            state='confirmed',materials='unknown',assigned_to=None,crew_assignment=None,source=source,**fields)
        for data in row['defects']:
            defect=Defect.objects.create(contract=contract,section=job.section,source={'historical_import':True,'archive_payload_sha256':p['digest']},**data)
            JobDefect.objects.create(job=job,defect=defect)
        if row['date']:
            Report.objects.create(job=job,author=fresh,kind='complete',performed_date=row['date'],
                method='' if source.get('actual_method_unknown') else job.method,length_m=job.length_m,
                composition=job.composition,coupling_snapshot=source.get('component_snapshots',[]),
                note='Исторический перенос. Автор записи — оператор; исполнитель в исходных данных не установлен.',
                context={'historical_import':True,'actual_coating_length_m':None})
    batch.applied_at=timezone.now();batch.save(update_fields=['applied_at'])
    with lifecycle_write():
        contract.status='archived';contract.version+=1;contract.save(update_fields=['status','version'])
    ContractClosureEvent.objects.create(contract=contract,actor=fresh,action='archive',previous_status='planned',note='Перенесён подтверждённый исторический реестр. Действующие работы не изменялись.')
    audit(fresh,'historical_repairs_imported',entity_id=str(contract.pk),batch=str(batch.pk),**counts(p))
    return counts(p)

@transaction.atomic
def cancel(user,pk):
    actor(user,True);candidate=batch_for(user,pk)
    contract=Contract.objects.select_for_update(no_key=True).get(pk=candidate.contract_id)
    batch=ImportBatch.objects.select_for_update(of=('self',)).get(pk=pk)
    if batch.applied_at or contract.status=='archived':return False
    if not batch.payload.get('cancelled'):
        batch.payload=dict(batch.payload,cancelled=True);batch.save(update_fields=['payload'])
    return True
