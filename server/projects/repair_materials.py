"""Normative material demand and idempotent posting, separate from plan lengths."""
from collections import Counter
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
import hashlib,json,uuid
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from accounts.permissions import permitted
from repairs.construction import planned_parts,demand_parts
from repairs.models import Job
from .models import (CoatingProfile, RepairConsumption, StockItem, StockBalance, StockMovement,
 Warehouse, COMPANY_TAX_ID)
from . import stock, stock_fit

KITS=json.loads((Path(__file__).resolve().parent.parent/'repairs/rd_kits.json').read_text())
RATES={'tape':Decimal('5.19'),'wrapper':Decimal('.81'),'primer':Decimal('.21')}

def can_read(user):
    return user.is_authenticated and user.is_active and user.role!='customer' and (user.is_leader or permitted(user,'stock.manage') or permitted(user,'repairs.prepare') or permitted(user,'works.prepare'))

def kits(diameter,length_mm):
    key=str(diameter)
    if key not in KITS['seal'] or length_mm not in range(1000,10501,500):raise ValidationError('Для этой длины или диаметра нет значения в таблицах 3/5 РД. Нужен отдельный расчёт.')
    return {'sealant':KITS['seal'][key],'composite':KITS['composite'][key][(length_mm-1000)//500]}

def eligible(warehouse,job):
    if warehouse.archived:return False
    if warehouse.brigade_assignment_id:return warehouse.brigade_assignment_id==job.crew_assignment_id and warehouse.project_object_id==job.section.project_object_id
    served=list(warehouse.served_objects.all());crews=list(warehouse.served_brigades.all())
    if served or crews:
        return any(o.pk==job.section.project_object_id for o in served) or bool(job.crew_assignment_id and any(b.pk==job.crew_assignment.brigade_id for b in crews))
    return not warehouse.project_object_id or warehouse.project_object_id==job.section.project_object_id


def coating(job,pieces=None):
    length=sum(c.length_mm for c in pieces) if pieces else int(Decimal(job.length_m or 0)*1000)
    profile=CoatingProfile.objects.select_related('tape','wrapper','primer').filter(active=True,method=job.method,diameter_mm=job.section.diameter_mm,length_mm=length).first()
    if not profile:raise ValidationError(f'Не настроена площадь покрытия: {job.get_method_display()} Ø{job.section.diameter_mm or "?"}, длина {length} мм. Укажите профиль изоляции один раз.')
    area=Decimal(profile.full_length_mm+profile.extra_length_mm)/1000*profile.area_per_m
    lines=[]
    for name,rate in RATES.items():
        item=getattr(profile,name)
        if item.unit!='kg' or item.archived or item.deleted_at:raise ValidationError('В профиле покрытия выберите действующие позиции Ксизола, обёртки и праймера в кг. Старые м² автоматически не переводятся.')
        lines.append({'kind':name,'item':str(item.pk),'name':item.name,'quantity':str((area*rate).quantize(Decimal('.001'),rounding=ROUND_CEILING)),'unit':'кг','area':str(area),'rate':str(rate),'profile':profile.pk,'profile_version':profile.version,'basis':profile.basis,'full_length_mm':profile.full_length_mm})
    return lines


def p1_kits(job,pieces=None):
    from .p1_coating import dimensions
    values=kits(job.section.diameter_mm,dimensions(job,pieces))
    return [{'kind':k,'name':'Герметик' if k=='sealant' else 'Композит','quantity':str(v),'unit':'компл.','source':'РД-108-10, табл. 3/5'} for k,v in values.items()]


def norm_groups(job,pieces=None):
    from .p1_coating import coating as p1_coating
    providers=[p1_kits,p1_coating] if job.method=='p1' else [coating] if job.method=='p2' else []
    for provider in providers:
        try:yield provider(job,pieces),[]
        except ValidationError as error:yield [],error.messages


def norm(job,pieces=None):
    lines=[];errors=[]
    for group,issues in norm_groups(job,pieces):lines.extend(group);errors.extend(issues)
    if errors:raise ValidationError(errors)
    return lines


def matches(line,item):
    units=('kg',) if line['unit']=='кг' else ('kit','pcs')
    return item.unit in units and (str(item.pk)==line['item'] if line.get('item') else item.kind==line['kind'])


def _fingerprint(job,pieces):
    return hashlib.sha256(json.dumps([job.method,job.section.diameter_mm,sorted((str(c.pk),c.length_mm,str(c.wall_mm)) for c in pieces)]+([format(Decimal(job.actual_cleaning_length_m).normalize(),'f')] if getattr(job,'actual_cleaning_length_m',None) is not None else []),sort_keys=True).encode()).hexdigest()


def _post(actor,job,report,lines):
    warehouses=list(Warehouse.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID).prefetch_related('served_objects','served_brigades'))
    permitted_wh={w.pk for w in warehouses if eligible(w,job)}
    # Resolve all legs before writing any. A shortage never consumes half a set.
    selected=Q(item_id__in=[line['item'] for line in lines if line.get('item')])|Q(item__kind__in=[line['kind'] for line in lines if not line.get('item')])
    balances=list(StockBalance.objects.select_for_update(of=('self',)).filter(selected,
        warehouse_id__in=permitted_wh,quantity__gt=0,item__legal_entity__tax_id=COMPANY_TAX_ID,
        item__archived=False,item__deleted_at__isnull=True,item__reserved_job__isnull=True,
    ).select_related('item','warehouse').order_by('warehouse_id','item_id'))
    left={balance.pk:balance.quantity for balance in balances};legs=[]
    for index,line in enumerate(lines):
        amount=Decimal(line['quantity'])
        for balance in balances:
            if not matches(line,balance.item):continue
            take=min(left[balance.pk],amount)
            if take:legs.append((index,balance.item,balance.warehouse,take));amount-=take;left[balance.pk]-=take
            if not amount:break
        if amount:raise ValidationError('Не хватает материала для списания: '+line['name']+'. Ремонт сохранён; уточните остатки/склад и повторите списание.')
    movements=[]
    for line in lines:line['movements']=[];line['allocations']=[]
    for n,(index,item,wh,q) in enumerate(legs):
        row=stock.post_locked(actor,item,'consume',q,wh,None,report.performed_date,'Автоматически по норме после ремонта '+job.get_method_display(),uuid.uuid5(report.token,'coating:'+str(n)),job,report,automatic=True);movements.append(str(row.pk))
        lines[index]['movements'].append(str(row.pk));lines[index]['allocations'].append({'item':str(item.pk),'warehouse':str(wh.pk),'quantity':str(q),'movement':str(row.pk)})
    return movements


def reconcile(actor,job,report,pieces):
    """Caller owns stock lock and repair transaction. Missing norms do not erase work."""
    if job.method not in ('p1','p2') or report.outcome!='complete':return
    job.actual_cleaning_length_m=report.context.get('actual_cleaning_length_m')
    fingerprint=_fingerprint(job,pieces)
    previous=RepairConsumption.objects.select_for_update().filter(job=job).first()
    if previous and previous.state=='posted':
        if previous.fingerprint==fingerprint:return previous
        previous.state='review';previous.review_reason='installation';previous.message='Изменена установленная конструкция. Ранее списанные материалы сохранены; требуется проверка расхода руководителем.';previous.save(update_fields=['state','review_reason','message','updated_at']);return previous
    if previous and previous.state=='review' and previous.review_reason!='manual':return previous
    if job.method=='p1' and previous is None and job.reports.filter(outcome='complete').exclude(pk=report.pk).exists():
        return RepairConsumption.objects.create(job=job,report=report,fingerprint=fingerprint,state='review',review_reason='legacy',lines=[],message='П1 уже была выполнена до подключения табличного расхода. Автоматическое списание задним числом не проводится; проверьте прежний фактический расход.')
    if job.method=='p1' and StockMovement.objects.filter(job=job,kind='consume',automatic=False,reversal__isnull=True,item__kind__in=['sealant','composite','primer','ksizol','wrapper']).exists():
        return RepairConsumption.objects.update_or_create(job=job,defaults={'report':report,'fingerprint':fingerprint,'state':'review','review_reason':'manual','lines':previous.lines if previous else [],'message':'Для этой П1 уже есть ручное списание материалов. Оно сохранено; повторное автоматическое списание не выполнено. ПТО должен сверить расход.'})[0]
    lines=[]
    try:
        if not pieces:raise ValidationError('Не указана установленная муфта. Ремонт сохранён; проверьте фактическую конструкцию перед списанием изоляции.')
        lines=norm(job,pieces)
        with transaction.atomic():_post(actor,job,report,lines)
        state='posted';message='Материалы списаны автоматически по рабочей норме.'
    except ValidationError as e:state='pending';message='; '.join(e.messages)
    return RepairConsumption.objects.update_or_create(job=job,defaults={'report':report,'fingerprint':fingerprint,'state':state,'review_reason':'','lines':lines,'message':message})[0]

@transaction.atomic
def retry(user,job):
    actor=stock.manager(user);stock.lock()
    from repairs.access import visible_jobs
    job=visible_jobs(actor).select_for_update(of=('self',)).get(pk=job.pk)
    row=RepairConsumption.objects.select_for_update().filter(job=job).first()
    if not row or not (row.state=='pending' or row.state=='review' and row.review_reason=='manual'):raise ValidationError('Нет ожидающего списания. Проверьте историю расхода.')
    pieces=[u.coupling for u in job.installed_couplings.select_related('coupling')]
    return reconcile(actor,job,row.report,pieces)


def all_demand(user=None):
    """Shared physical stock is allocated once; output is projected per viewer later."""
    from collections import defaultdict,deque
    from repairs.construction import COUPLING_METHODS
    jobs=list(Job.objects.filter(method__in=COUPLING_METHODS,section__project_object__legal_entity__tax_id=COMPANY_TAX_ID).exclude(state__in=['reported','confirmed','unrepairable']).exclude(contract__status='archived').exclude(section__project_object__status='archived').select_related('section','section__project_object','crew_assignment','contract').prefetch_related('defects','installed_couplings__coupling','reserved_stock'))
    warehouses={w.pk:w for w in Warehouse.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID).prefetch_related('served_objects','served_brigades')}
    supply=[]
    if user is not None:
        accessible=set(stock.warehouses(user).values_list('pk',flat=True));warehouses={pk:w for pk,w in warehouses.items() if pk in accessible}
    for b in StockBalance.objects.filter(item__legal_entity__tax_id=COMPANY_TAX_ID,item__archived=False,item__deleted_at__isnull=True,quantity__gt=0).select_related('item'):
        wh=warehouses.get(b.warehouse_id)
        if wh:supply.append({'item':b.item,'warehouse':wh,'left':b.quantity})
    jobs.sort(key=lambda j:(j.planned_date or timezone.localdate(),str(j.pk)))
    rows=[];nodes=[];graph=defaultdict(dict);source=('s',);sink=('t',)
    def edge(a,b,q):graph[a][b]=q;graph[b][a]=0
    for index,s in enumerate(supply):
        if s['item'].kind=='coupling' and not s['item'].reserved_job_id:edge(source,('stock',index),int(s['left']))
    for job in jobs:
        row={'job':job,'couplings':[],'materials':[],'issues':[]};rows.append(row)
        try:
            installed=[u.coupling for u in job.installed_couplings.all()]
            total,parts=demand_parts(job.length_m,job.composition,job.method)
            if job.method=='p1' and installed:
                if any(stock_fit.reason(c,job,False) for c in installed):raise ValidationError('Параметры установленной муфты отличаются от задания. Проверьте фактический результат.')
                left=max(Decimal(0),total-sum(Decimal(c.length_mm)/1000 for c in installed))
                parts=demand_parts(left,'',job.method)[1] if left else []
            if job.method=='p2' and len(parts)>1:raise ValidationError('П2 нельзя стыковать. Уточните прежний состав задания.')
            if job.method=='p1' and total>Decimal('10.5'):raise ValidationError('П1 длиннее рабочего ограничения 10,5 м. Требуется проверка задания.')
            if not job.section.diameter_mm or not job.wall_mm:raise ValidationError('Укажите диаметр участка и стенку конструкции.')
            initial=Counter(int(p*1000) for p in parts) if job.method=='p1' and installed else stock_fit.consume(job,installed,Counter(int(p*1000) for p in parts),check_diameter=False)
            held=[s['item'] for s in supply if s['item'].reserved_job_id==job.pk]
            remaining=stock_fit.consume(job,held,initial)
            walls={Decimal(i.wall_mm) for i in held+installed if i.wall_mm}
            if job.method=='p1' and len(walls)>1:raise ValidationError('В закреплении составной П1 смешаны стенки. Требуется уточнить распределение.')
            candidates=[(n,s) for n,s in enumerate(supply) if s['item'].kind=='coupling' and not s['item'].reserved_job_id and eligible(s['warehouse'],job) and not stock_fit.reason(s['item'],job)]
            if job.method=='p1' and len(parts)>1:
                if walls:wall=next(iter(walls))
                else:
                    possible=sorted({Decimal(s['item'].wall_mm) for n,s in candidates})
                    def score(w):
                        left=Counter(remaining)
                        for n,s in sorted(candidates,key=lambda x:x[1]['item'].length_mm):
                            if Decimal(s['item'].wall_mm)!=w:continue
                            for _ in range(min(int(s['left']),sum(left.values()))):left=stock_fit.consume(job,[s['item']],left)
                        return sum(remaining.values())-sum(left.values())
                    wall=min(possible,key=lambda w:(-score(w),w)) if possible else job.wall_mm
                candidates=[(n,s) for n,s in candidates if Decimal(s['item'].wall_mm)==wall]
            for length,count in initial.items():
                if not count:continue
                line={'method':job.method,'diameter':job.section.diameter_mm,'length':length,'wall':job.wall_mm,'name':f'{job.get_method_display()} Ø{job.section.diameter_mm} · {length} × {job.wall_mm} мм','need':count,'reserved':count-remaining[length],'missing':remaining[length],'unit':'шт.'};row['couplings'].append(line)
                if not remaining[length]:continue
                node=('need',len(nodes));nodes.append((node,line));edge(node,sink,remaining[length])
                for n,s in candidates:
                    if stock_fit.length_fits(job.method,s['item'].length_mm,length):edge(('stock',n),node,min(int(s['left']),remaining[length]))
        except ValidationError as e:row['issues'].extend(e.messages)
    while True:
        parent={source:None};queue=deque([source])
        while queue and sink not in parent:
            n=queue.popleft()
            for to,q in graph[n].items():
                if q>0 and to not in parent:parent[to]=n;queue.append(to)
        if sink not in parent:break
        n=sink;q=None
        while parent[n] is not None:
            frm=parent[n];q=graph[frm][n] if q is None else min(q,graph[frm][n]);n=frm
        n=sink
        while parent[n] is not None:
            frm=parent[n];graph[frm][n]-=q;graph[n][frm]+=q;n=frm
    for node,line in nodes:line['missing']=graph[node][sink]
    for row in rows:row['coupling_issues']=list(row['issues'])
    # Material allocation uses the same residual matching principle as couplings:
    # a generic stock location must not starve a job restricted to that location.
    graph=defaultdict(dict);nodes=[]
    for index,s in enumerate(supply):
        if s['item'].kind!='coupling' and not s['item'].reserved_job_id:edge(source,('material_stock',index),s['left'])
    for row in rows:
        job=row['job']
        for lines,issues in norm_groups(job):
            row['issues'].extend(issues)
            for line in lines:
                amount=Decimal(line['quantity']);line={**line,'need':amount,'missing':amount};row['materials'].append(line)
                node=('material_need',len(nodes));nodes.append((node,line));edge(node,sink,amount)
                for n,s in enumerate(supply):
                    item=s['item']
                    if matches(line,item) and not item.reserved_job_id and eligible(s['warehouse'],job):edge(('material_stock',n),node,min(s['left'],amount))
    while True:
        parent={source:None};queue=deque([source])
        while queue and sink not in parent:
            n=queue.popleft()
            for to,q in graph[n].items():
                if q>0 and to not in parent:parent[to]=n;queue.append(to)
        if sink not in parent:break
        n=sink;q=None
        while parent[n] is not None:
            frm=parent[n];q=graph[frm][n] if q is None else min(q,graph[frm][n]);n=frm
        n=sink
        while parent[n] is not None:
            frm=parent[n];graph[frm][n]-=q;graph[n][frm]+=q;n=frm
    for node,line in nodes:line['missing']=graph[node][sink]
    return rows
