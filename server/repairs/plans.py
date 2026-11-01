"""Change required construction; physical stock and actual installation stay intact."""
from collections import Counter
from decimal import Decimal
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.utils import timezone
from accounts.models import User
from accounts.views import audit
from projects import stock,stock_fit
from projects.models import StockItem
from .access import can_prepare,visible_jobs
from .construction import planned_parts,COUPLING_METHODS
from .models import Job,Report,METHODS

FIELDS=('method','length_m','wall_mm','composition')

def snapshot(job):
    return {k:(format(Decimal(str(getattr(job,k))).normalize(),'f') if k in ('length_m','wall_mm') and getattr(job,k) is not None else str(getattr(job,k) or '')) for k in FIELDS}

def validate_change(current,target):
    if snapshot(current)==snapshot(target):return
    if current.state in ('reported','confirmed') or current.installed_couplings.exists():raise ValidationError('Уже внесён фактический монтаж. Изменяйте его через исправление результата; прежняя история сохранится.')
    if current.contract and current.contract.status=='archived' or current.section.project_object.status=='archived':raise ValidationError('Задание находится в архиве.')
    if current.defects.filter(archived_at__isnull=False).exists():raise ValidationError('Сначала восстановите удалённые дефекты задания.')

def record_change(actor,job,before,note=''):
    after=snapshot(job)
    if not before or before==after:return []
    parts=planned_parts(job.length_m,job.composition,job.method)[1] if job.method in COUPLING_METHODS else []
    left=Counter(int(p*1000) for p in parts)
    held=list(StockItem.objects.select_for_update().filter(reserved_job=job,legal_entity=job.section.project_object.legal_entity).order_by('length_mm','pk'))
    mixed=job.method=='p1' and len(parts)>1 and len({i.wall_mm for i in held})>1
    released=[]
    for item in held:
        remaining=stock_fit.consume(job,[item],left) if not mixed else left
        if mixed or remaining is None or sum(remaining.values())==sum(left.values()):
            released.append({'id':str(item.pk),'serial':item.serial,'length_mm':item.length_mm,'wall_mm':str(item.wall_mm or '')})
            item.reserved_job=None;item.reservation_origin='';item.version+=1;item.save(update_fields=['reserved_job','reservation_origin','version'])
            audit(actor,'stock_reserved',item=str(item.pk),job='',previous_job=str(job.pk),reason='Изменены требования конструкции')
        else:left=remaining
    job.materials='unknown';job.save(update_fields=['materials'])
    Report.objects.create(job=job,author=actor,kind='plan_change',performed_date=timezone.localdate(),note=note.strip() or 'Уточнены параметры задания.',context={'previous_plan':dict(before,method_label=dict(METHODS).get(before['method'],before['method'])),'new_plan':dict(after,method_label=job.get_method_display()),'released_reservations':released})
    audit(actor,'repair_prepared',kind='construction_changed',entity_id=str(job.pk),previous=before,current=after,released=[r['id'] for r in released])
    return released

@transaction.atomic
def revise(user,pk,expected_version,values,reason):
    actor=User.objects.select_for_update(no_key=True).get(pk=user.pk)
    if not can_prepare(actor) or actor.session_version!=user.session_version:raise PermissionDenied
    stock.lock()
    candidate=visible_jobs(actor).get(pk=pk)
    if candidate.contract_id:
        from projects.models import Contract
        Contract.objects.select_for_update(no_key=True).get(pk=candidate.contract_id)
    job=visible_jobs(actor).select_for_update(no_key=True,of=('self',)).get(pk=pk)
    if job.version!=expected_version:raise ValidationError('Задание уже изменено. Обновите карточку.')
    if not reason.strip():raise ValidationError('Кратко укажите основание изменения.')
    before=snapshot(job)
    previous=Job.objects.get(pk=job.pk)
    for key in FIELDS:setattr(job,key,values[key])
    validate_change(previous,job)
    if job.method in COUPLING_METHODS:job.length_m,_=planned_parts(job.length_m,job.composition,job.method)
    if before==snapshot(job):return job,[]
    job.version+=1;job.save(update_fields=[*FIELDS,'version'])
    return job,record_change(actor,job,before,reason)
