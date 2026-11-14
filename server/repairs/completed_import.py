"""Reviewed insert-only completed repairs in a pinned active contract.

Existing working records are authoritative: completed matches are skipped and
other matches are held for manual review. No stock or assignment writes; dated
new imports receive an ordinary report with explicit import provenance.
"""
import hashlib,json,re,uuid,math
from datetime import date,timedelta
from decimal import Decimal,InvalidOperation
from django.core.exceptions import ValidationError,PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction,IntegrityError
from django.db.models import Q
from django.utils import timezone
from accounts.views import audit
from projects.models import Contract,COMPANY_TAX_ID
from projects.access import visible_contracts
from documents.storage import validate_upload,store_file,private_path
from .models import Job,Defect,JobDefect,ImportBatch,Report
from .archive_import import actor,allowed,norm,digest,scalar,object_pairs,resolve
from .values import kilometre,identifier

MARKER='completed_register_2026_v1'
FORMAT='armada-completed-2026-plan-v1'
MAX_BYTES=3*1024*1024
NUMBER_TOKEN='ТЗС-1731-2025'


def target(user,pk,lock=False):
    actor(user)
    qs=Contract.objects.filter(pk=pk,legal_entity__tax_id=COMPANY_TAX_ID,pk__in=visible_contracts(user).values('pk'))
    if lock:qs=qs.select_for_update(of=('self',),no_key=True)
    contract=qs.first()
    if not contract:raise PermissionDenied
    number=re.sub('[—–−]','-',contract.number)
    if contract.status!='active' or not re.search(r'(?<![\w-])ТЗС-1731-2025(?![\w-])',number):
        raise ValidationError('Нужен действующий контракт ТЗС-1731-2025. Архивный договор не подходит.')
    return contract


def read(upload,contract):
    raw=upload.read(MAX_BYTES+1);upload.seek(0)
    try:
        if not raw or len(raw)>MAX_BYTES:raise ValueError
        p=json.loads(raw.decode('utf-8-sig'),object_pairs_hook=object_pairs)
        if p['format']!=FORMAT or str(uuid.UUID(p['target_contract']['id']))!=str(contract.pk) or p['target_contract']['number_token']!=NUMBER_TOKEN:raise ValueError
        rows=p['jobs']
        if not isinstance(rows,list) or not 1<=len(rows)<=500 or p['payload_sha256']!=digest(rows):raise ValueError
        def bounded(v,depth=0):
            if depth>14:raise ValueError
            if isinstance(v,dict):
                if len(v)>150:raise ValueError
                for k,x in v.items():
                    if not isinstance(k,str) or len(k)>200:raise ValueError
                    bounded(x,depth+1)
            elif isinstance(v,list):
                if len(v)>1000:raise ValueError
                for x in v:bounded(x,depth+1)
            elif isinstance(v,str) and len(v)>10000:raise ValueError
            elif isinstance(v,float) and not math.isfinite(v):raise ValueError
        bounded(p)
        bindings=resolve(contract,[{'project_object_name':r['object']} for r in rows])
        clean=[];seen=set()
        for r in rows:
            if not isinstance(r,dict) or not isinstance(r.get('fields'),dict):raise ValueError
            fields={k:scalar(Job,k,r['fields'].get(k)) for k in ('method','length_m','wall_mm','composition','notes')}
            for key in ('length_m','wall_mm'):
                if fields[key] is not None and fields[key]<=0:raise ValueError
            if not isinstance(r['defects'],list) or len(r['defects'])!=1:raise ValueError
            # A shared construction requires explicit separate review; no inferred merge.
            d=r['defects'][0]
            if not isinstance(d,dict):raise ValueError
            d={'kilometre':str(kilometre(d['kilometre'])),'number':identifier(d['number'],'№ дефекта'),
               'pipe_section':identifier(d.get('pipe_section',''),'№ секции',optional=True)}
            key=(bindings[r['object']],d['kilometre'],d['number'])
            if key in seen:raise ValueError
            seen.add(key)
            day=date.fromisoformat(r['date']) if r.get('date') else None
            if day and (day.year!=2026 or day>timezone.localdate()):raise ValueError
            src=r['source']
            if not isinstance(src,dict) or src.get('period')!='2026' or src.get('completion_date')!=(day.isoformat() if day else None):raise ValueError
            members=src.get('source_members')
            if not isinstance(members,list) or not 1<=len(members)<=50:raise ValueError
            for member in members:
                if not isinstance(member,dict) or not isinstance(member.get('evidence'),dict) or member['evidence'].get('kind') not in ('green_kilometre','owner_confirmed','completion_fact'):raise ValueError
                for key in ('source_refs','notes','review'):
                    values=member.get(key,[])
                    if not isinstance(values,list) or len(values)>100 or any(not isinstance(v,str) or len(v)>1000 for v in values):raise ValueError
            parts=src.get('component_snapshots',[])
            if not isinstance(parts,list) or len(parts)>100:raise ValueError
            for part in parts:
                if not isinstance(part,dict):raise ValueError
                scalar(Job,'method',part.get('method'))
                mounted=date.fromisoformat(part['mount_date'])
                if mounted.year!=2026 or mounted>timezone.localdate():raise ValueError
                if not isinstance(part.get('length_mm'),int) or isinstance(part['length_mm'],bool) or not 0<part['length_mm']<100000:raise ValueError
                if part.get('wall_mm') is not None and scalar(Job,'wall_mm',part['wall_mm'])<=0:raise ValueError
                if not isinstance(part.get('serial_as_written',''),str):raise ValueError
            # Do not accept opaque flags which grant historic/archive semantics.
            src={k:src[k] for k in ('period','completion_date','original_register_length_m','original_register_composition',
                 'original_wall_as_written','component_snapshots','source_members','original_source_rows','source_workbook_sha256') if k in src}
            src.update(completed_register_import=True,actual_coating_length_m=None,completed_import_key=hashlib.sha256(json.dumps(key).encode()).hexdigest())
            fields={k:str(v) if isinstance(v,Decimal) else v for k,v in fields.items()}
            clean.append({'object':r['object'],'section':bindings[r['object']],'defect':d,'fields':fields,'source':src,'date':day.isoformat() if day else None})
    except (ValueError,KeyError,TypeError,RecursionError,UnicodeError,InvalidOperation,ValidationError):
        raise ValidationError('Проверьте подготовленный план 2026: целевой договор, участки, факт выполнения и поля. Ничего не перенесено.') from None
    return clean,raw,p['payload_sha256'],bindings


def classify(contract,rows):
    result=[]
    for r in rows:
        d=r['defect'];km=Decimal(d['kilometre'])
        match=Q(kilometre__gte=km-Decimal('.0005'),kilometre__lt=km+Decimal('.0005'),number=d['number'])
        if d['number']!='0':match|=Q(number=d['number'])
        if d['pipe_section']:match|=Q(pipe_section=d['pipe_section'],kilometre__gte=km-Decimal('.001'),kilometre__lte=km+Decimal('.001'))
        found=list(Defect.objects.filter(match,contract=contract,section_id=r['section']).order_by('pk'))
        status='create';existing=[]
        if found:
            status='hold'
            exact=len(found)==1 and kilometre(found[0].kilometre)==kilometre(km) and found[0].number==d['number'] and found[0].pipe_section==d['pipe_section'] and not found[0].archived_at
            jobs=list(Job.objects.filter(defects__in=found).distinct().order_by('pk'))
            if exact and jobs and all(j.state=='confirmed' and j.contract_id==contract.pk and j.section_id==uuid.UUID(r['section']) for j in jobs):status='skip'
            existing=[{'defect':str(v.pk),'snapshot':digest([v.source,str(v.kilometre),v.number,v.pipe_section,str(v.archived_at)])} for v in found]
            existing += [{'job':str(v.pk),'snapshot':digest([v.source,v.state,v.version,v.method,str(v.length_m),str(v.wall_mm),v.composition,v.notes,str(v.assigned_to_id),str(v.crew_assignment_id),str(v.accepted_date),v.review_note,v.problem,v.problem_note])} for v in jobs]
        result.append({'status':status,'existing':existing})
    return result


def totals(batch):
    states=batch.payload['classification']
    return {'create':sum(s['status']=='create' for s in states),'skip':sum(s['status']=='skip' for s in states),
            'hold':sum(s['status']=='hold' for s in states),'undated':sum(not r['date'] and s['status']=='create' for r,s in zip(batch.payload['rows'],states))}


def summary(batch):
    n=totals(batch)
    return batch.contract.number+f'\nДобавить устранённых: {n["create"]}. Уже есть — пропустить: {n["skip"]}. Совпадения для проверки — не менять: {n["hold"]}. Без известной даты: {n["undated"]}.\nДействующий договор остаётся в работе. Существующие результаты, назначения, склад и права не меняются.'


def batch_for(user,pk):
    actor(user)
    batch=ImportBatch.objects.select_related('contract').filter(pk=pk,author=user,contract__in=visible_contracts(user)).first()
    if not batch or batch.payload.get('type')!=MARKER:raise PermissionDenied
    return batch


def prepare(user,contract_id,upload,stored_keys=None):
    saved=None
    try:
        with transaction.atomic():
            fresh=actor(user,True);contract=target(fresh,contract_id,True)
            rows,raw,sha,bindings=read(upload,contract)
            old=ImportBatch.objects.filter(contract=contract,author=fresh,payload__type=MARKER,payload__digest=sha,applied_at__isnull=False).first()
            if old:return old
            classification=classify(contract,rows)
            data=validate_upload(SimpleUploadedFile(upload.name,raw));saved=store_file(data)
            if stored_keys is not None:stored_keys.append(saved)
            payload={'type':MARKER,'digest':sha,'rows':rows,'classification':classification,'bindings':bindings,'cancelled':False}
            return ImportBatch.objects.create(contract=contract,section_id=next(iter(bindings.values())),author=fresh,
                filename=data['name'],storage_key=saved,size=data['size'],sha256=data['sha256'],sheet='Выполнено 2026',payload=payload)
    except Exception:
        if saved:private_path(saved).unlink(missing_ok=True)
        raise


@transaction.atomic
def _apply(user,pk,expected_digest):
    fresh=actor(user,True);candidate=batch_for(fresh,pk);contract=target(fresh,candidate.contract_id,True)
    batch=ImportBatch.objects.select_for_update(of=('self',)).get(pk=pk);p=batch.payload
    if p['digest']!=expected_digest or p.get('cancelled'):raise ValidationError('Предпросмотр отменён или изменён.')
    if batch.applied_at:return totals(batch)
    if timezone.now()-batch.created_at>timedelta(hours=24):raise ValidationError('Предпросмотр устарел. Загрузите план снова.')
    bindings=resolve(contract,[{'project_object_name':r['object']} for r in p['rows']])
    if bindings!=p['bindings'] or classify(contract,p['rows'])!=p['classification']:
        raise ValidationError('База изменилась после проверки. Загрузите план заново; существующие записи не менялись.')
    for r,s in zip(p['rows'],p['classification']):
        if s['status']!='create':continue
        source=dict(r['source'],completed_import_digest=p['digest'])
        job=Job.objects.create(contract=contract,section_id=r['section'],created_by=fresh,state='confirmed',source=source,**r['fields'])
        defect=Defect.objects.create(contract=contract,section_id=r['section'],source={'completed_register_import':True,'completed_import_digest':p['digest']},**r['defect'])
        JobDefect.objects.create(job=job,defect=defect)
        if r['date']:
            Report.objects.create(job=job,author=fresh,kind='complete',performed_date=r['date'],method=job.method,
                length_m=job.length_m,composition=job.composition,coupling_snapshot=source.get('component_snapshots',[]),
                note='Перенос выполненного ремонта из ведомости 2026. Автор записи — оператор переноса; исполнитель в источнике не установлен.',
                context={'completed_register_import':True,'actual_coating_length_m':None})
    batch.applied_at=timezone.now();batch.save(update_fields=['applied_at'])
    audit(fresh,'completed_register_imported',entity_id=str(contract.pk),batch=str(batch.pk),**totals(batch))
    return totals(batch)


def apply(user,pk,expected_digest):
    try:return _apply(user,pk,expected_digest)
    except IntegrityError:
        raise ValidationError('Во время переноса появились совпадающие записи. Ничего не перенесено; проверьте план заново.') from None


@transaction.atomic
def cancel(user,pk):
    actor(user,True);candidate=batch_for(user,pk);target(user,candidate.contract_id,True)
    batch=ImportBatch.objects.select_for_update(of=('self',)).get(pk=pk)
    if batch.applied_at:return False
    batch.payload=dict(batch.payload,cancelled=True);batch.save(update_fields=['payload']);return True
