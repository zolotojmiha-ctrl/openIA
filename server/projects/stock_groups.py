"""A presentation group is a warehouse + material specification, not a new ledger."""
import hashlib,json,uuid
from collections import defaultdict
from decimal import Decimal
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from . import stock,stock_planning as planning
from .models import StockItem

SALT='warehouse-material-group-v619'

def specification(item):
    dimensions=[item.method,item.diameter_mm,item.length_mm,item.wall_mm] if item.kind=='coupling' else ['',None,None,None]
    electrode=[item.electrode_brand.strip().casefold(),format(item.electrode_diameter.normalize(),'f') if item.electrode_diameter else ''] if item.kind=='electrode' else ['','']
    return [item.kind,item.unit,*dimensions,*electrode,'' if item.kind=='coupling' else item.name.strip().casefold()]

def identity(item,warehouse_id):
    return hashlib.sha256(json.dumps([str(warehouse_id or ''),*specification(item)],ensure_ascii=False).encode()).hexdigest()

def groups(user,warehouse=None):
    from .stock_planning_views import inventory_rows
    result={}
    for row in inventory_rows(user,warehouse):
        item=row['item'];legs=[(b.warehouse,b.quantity) for b in row['balances']]
        if not legs:
            # Keep an installed piece with its last actual stock location.
            last=stock.movements(user).filter(item=item,source__isnull=False).first()
            wh=last.source if last else row['receipt_warehouse']
            legs=[(wh,Decimal(0))]
        for wh,q in legs:
            if warehouse and (not wh or wh.pk!=warehouse.pk):continue
            key=identity(item,wh.pk if wh else None)
            group=result.setdefault(key,{'id':key,'item':item,'warehouse':wh,'members':[],'quantity':Decimal(0),'reserved':Decimal(0),'free':Decimal(0),'installed':0,'distribution':{},'search':''})
            member={**row,'quantity':q,'warehouse':wh}
            group['members'].append(member);group['quantity']+=q
            if row['state']=='installed':group['installed']+=1
            elif item.reserved_job_id:group['reserved']+=q
            else:group['free']+=q
            if row['job']:
                state='installed' if row['state']=='installed' else 'reserved'
                bucket=group['distribution'].setdefault((str(row['job'].pk),state),{'job':row['job'],'label':row['job_label'],'state':state,'quantity':Decimal(0)})
                bucket['quantity']+=1 if state=='installed' else q
            group['search']+=' '+row['job_label']+' '+row['serial']
    for g in result.values():
        records=[]
        for m in g['members']:
            i=m['item']
            latest=i.movements.order_by('-created_at','-pk').values_list('pk',flat=True).first()
            records.append([str(i.pk),i.version,str(m['quantity']),str(i.reserved_job_id or ''),m['state'],str(latest or ''),m['serial'],m['receipt_source'],str(m['receipt_date'] or ''),str(m['installed_date'] or ''),m['job_label']])
        g['fingerprint']=hashlib.sha256(json.dumps(sorted(records),ensure_ascii=False).encode()).hexdigest()
        g['token']=signing.dumps({'actor':str(user.pk),'group':g['id'],'fingerprint':g['fingerprint']},salt=SALT)
        g['distribution']=list(g['distribution'].values())
        g['search']=(g['item'].name+' '+g['item'].method_label+' '+g['item'].electrode_brand+' '+(g['warehouse'].name if g['warehouse'] else '')+g['search']).casefold()
    return sorted(result.values(),key=lambda g:(g['item'].name.casefold(),g['warehouse'].name if g['warehouse'] else '',g['id']))

def current(user,token,known=None):
    try:
        payload=signing.loads(token,salt=SALT,max_age=8*3600)
        if payload['actor']!=str(user.pk):raise ValueError
        group=next((g for g in (known if known is not None else groups(user)) if g['id']==payload['group']),None)
        if not group or group['fingerprint']!=payload['fingerprint']:raise ValueError
    except (signing.BadSignature,KeyError,TypeError,ValueError):raise ValidationError('Остаток или распределение изменились. Скачайте свежий Excel / откройте строку заново.')
    return group

@transaction.atomic
def distribute(user,token,targets):
    actor=stock.manager(user);stock.lock();g=current(actor,token)
    if g['item'].kind!='coupling' or not g['warehouse']:raise ValidationError('Распределять можно муфты с фактическим остатком на складе.')
    wh=g['warehouse']
    if any(Decimal(v)<0 or Decimal(v)!=Decimal(v).to_integral_value() for v in targets.values()):raise ValidationError('Для распределения нужны целые неотрицательные количества.')
    targets={str(k):int(v) for k,v in targets.items() if int(v)>0}
    existing={str(d['job'].pk):int(d['quantity']) for d in g['distribution'] if d['state']=='reserved'}
    if targets==existing:return g
    if sum(targets.values())>g['quantity']:raise ValidationError('Распределение превышает остаток на этом складе.')
    # Preserve factory-number identities already reserved for each job. Release
    # only the surplus, instead of shuffling every piece when one count changes.
    kept=defaultdict(int)
    for m in sorted(g['members'],key=lambda m:(not bool(m['serial']),str(m['item'].pk))):
        i=m['item']
        if i.reserved_job_id and m['quantity']>0:
            key=str(i.reserved_job_id)
            if kept[key]<targets.get(key,0):kept[key]+=1
            else:planning.reserve(actor,i.pk,i.version,None)
    def available():
        ids=[m['item'].pk for m in g['members'] if m['quantity']>0]
        return stock.items(actor).filter(pk__in=ids,balances__warehouse=wh,balances__quantity__gt=0,reserved_job__isnull=True).order_by('sku')
    for job_id,count in targets.items():
        for _ in range(count-kept[job_id]):
            item=available().first()
            if not item:raise ValidationError('Свободные изделия закончились.')
            balance=item.balances.get(warehouse=wh)
            if not item.individual and not item.coupling_id and (balance.quantity>1 or item.balances.filter(quantity__gt=0).count()>1):
                item=planning.split(actor,item.pk,item.version,wh.pk)
                g['members'].append({'item':item,'quantity':Decimal(1)})
            planning.reserve(actor,item.pk,item.version,job_id)
    return g

def values(item):
    return {k:getattr(item,k) for k in (*stock.ITEM_FIELDS,'serial','receipt_origin','electrode_brand','electrode_diameter')}

@transaction.atomic
def correct_quantity(user,token,target):
    actor=stock.manager(user);stock.lock();g=current(actor,token);delta=Decimal(target)-g['quantity']
    if not g['warehouse']:raise ValidationError('Укажите склад для поступления, а не карточку без остатка.')
    if delta==0:return
    if target<g['reserved']:raise ValidationError('Нельзя уменьшить остаток ниже закреплённого количества. Измените распределение.')
    if delta>0:
        data=values(g['item']);data['serial']=''
        planning.receive(actor,data,g['warehouse'],delta,timezone.localdate(),'Корректировка остатка из Excel',uuid.uuid4())
    else:
        remaining=-delta
        for m in g['members']:
            item=m['item']
            if item.reserved_job_id or m['quantity']<=0:continue
            take=min(remaining,m['quantity'])
            stock.move(actor,item.pk,'loss',take,g['warehouse'].pk,None,timezone.localdate(),'Корректировка остатка из Excel',uuid.uuid4())
            remaining-=take
            if not remaining:break
        if remaining:raise ValidationError('Для уменьшения остатка недостаточно свободного количества.')
