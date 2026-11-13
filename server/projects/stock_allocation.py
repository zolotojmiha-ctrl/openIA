"""Explicit approval of a global stock proposal, rechecked under the stock ledger lock."""
import uuid
from collections import Counter
from decimal import Decimal
from django.core import signing
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction
from accounts.models import AuditEvent
from accounts.views import audit
from repairs.access import visible_jobs
from repairs.construction import planned_parts
from . import stock,stock_planning,stock_fit
from .models import StockItem

SALT='global-coupling-allocation-v1'
LIMIT=20


def details(records,allowed):
    result=[]
    for row in records:
        job=row['job']
        if str(job.pk) not in allowed or row['coupling_issues'] or not row['stock_complete'] or not row['allocation']:continue
        pieces=[]
        for part in row['allocation']:
            item=part['item'];wh=part['warehouse']
            pieces.append({'item':str(item.pk),'version':item.version,'warehouse':str(wh.pk),'warehouse_version':wh.version,
                'quantity':part['quantity'],'available':str(part['available']),'length':item.length_mm,'wall':str(item.wall_mm)})
        result.append({'job':str(job.pk),'version':job.version,'method':job.method,'length':str(job.length_m),
            'wall':str(job.wall_mm),'composition':job.composition,'pieces':pieces})
    return result[:LIMIT]


def proposal(user,records,allowed):
    allowed={str(pk) for pk in allowed}
    plans=details(records,allowed)
    rows=[]
    for plan in plans:
        row=next(row for row in records if str(row['job'].pk)==plan['job'])
        from .demand_details import location
        parts=Counter((piece['length'],piece['wall']) for piece in plan['pieces'] for _ in range(piece['quantity']))
        rows.append({'job':row['job'],**location(row['job']),
            'parts':[{'length':length,'wall':wall,'quantity':quantity} for (length,wall),quantity in sorted(parts.items())],
            'warehouses':list(dict.fromkeys(part['warehouse'].name for part in row['allocation']))})
    from .demand_details import order
    rows.sort(key=order)
    value=signing.dumps({'actor':str(user.pk),'session':user.session_version,'scope':sorted(allowed),
        'plans':plans,'nonce':str(uuid.uuid4())},salt=SALT,compress=True) if plans else ''
    return rows,value


def planned_slots(job,lengths):
    target=int(Decimal(job.length_m)*1000)
    slots=sorted(lengths,reverse=True)
    extra=sum(slots)-target
    if extra<0 or target<len(slots):raise ValidationError('Комплект не покрывает требуемую длину.')
    for index,length in enumerate(slots):
        cut=min(extra,length-1);slots[index]-=cut;extra-=cut
    composition='+'.join(format((Decimal(length)/1000).normalize(),'f') for length in sorted(slots,reverse=True))
    if extra or len(composition)>200:raise ValidationError('Состав слишком большой. Уточните конструкцию.')
    planned_parts(job.length_m,composition,'p1')
    return composition


@transaction.atomic
def apply(user,value):
    actor=stock.manager(user);stock.lock()
    try:data=signing.loads(value,salt=SALT,max_age=600)
    except signing.BadSignature:raise ValidationError('Подбор устарел. Обновите страницу и проверьте комплекты заново.') from None
    if data.get('actor')!=str(actor.pk) or data.get('session')!=actor.session_version:raise PermissionDenied
    scope=set(data.get('scope',[]))
    current=visible_jobs(actor).exclude(state__in=['reported','confirmed','unrepairable']).exclude(contract__status='archived').exclude(section__project_object__status='archived')
    visible={str(pk) for pk in current.filter(pk__in=scope).values_list('pk',flat=True)}
    if visible!=scope:raise PermissionDenied
    previous=AuditEvent.objects.filter(actor=actor,action='stock_reserved',details__kind='global_allocation',details__nonce=data['nonce']).first()
    if previous:return len(previous.details['jobs'])
    from .repair_materials import all_demand
    records=all_demand(actor)
    if data.get('plans')!=details(records,scope):raise ValidationError('Работы, остатки или распределение изменились. Обновите подбор перед закреплением.')
    from .archive import writable
    for plan in data['plans']:
        row=next(row for row in records if str(row['job'].pk)==plan['job'])
        job=current.select_for_update(of=('self',)).get(pk=plan['job']);writable([job.contract_id])
        if job.method=='p1':
            reserved,installed=stock_fit.occupied(job)
            lengths=[piece.length_mm for piece in reserved+installed]+[piece['length'] for piece in plan['pieces'] for _ in range(piece['quantity'])]
            if sum(piece.length_mm for piece in reserved+installed)!=row['_covered']:raise ValidationError('Уточните прежнее закрепление этой П1.')
            composition=planned_slots(job,lengths)
            if job.composition!=composition:
                before=job.composition;job.composition=composition;job.version+=1;job.save(update_fields=['composition','version'])
                audit(actor,'repair_prepared',entity_id=str(job.pk),kind='global_stock_composition',previous=before,composition=composition)
        for selected in plan['pieces']:
            for _ in range(selected['quantity']):
                item=stock.items(actor).get(pk=selected['item'])
                if not item.individual and not item.coupling_id:
                    balance=item.balances.filter(quantity__gt=0).first()
                    if not balance or item.balances.filter(quantity__gt=0).count()!=1 or balance.quantity!=1:
                        item=stock_planning.split(actor,item.pk,item.version,selected['warehouse'])
                stock_planning.reserve(actor,item.pk,item.version,job.pk)
    audit(actor,'stock_reserved',kind='global_allocation',nonce=data['nonce'],jobs=[plan['job'] for plan in data['plans']])
    return len(data['plans'])
