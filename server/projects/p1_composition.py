"""Human-selected P1 assemblies: exact sum, one wall, existing free pieces only."""
from collections import defaultdict
from decimal import Decimal
from itertools import product
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from accounts.views import audit
from repairs.access import visible_jobs
from . import stock,stock_planning
from .models import StockBalance,COMPANY_TAX_ID
from .repair_materials import eligible

SALT='p1-assembly-624'

def choices(job,user=None):
    if job.method!='p1' or not job.length_m or job.length_m>Decimal('10.5') or not job.wall_mm or not job.section.diameter_mm:return []
    if job.state in ('reported','confirmed','unrepairable') or job.reserved_stock.exists() or job.installed_couplings.exists():return []
    target=int(job.length_m*1000);groups=defaultdict(lambda:defaultdict(list))
    rows=StockBalance.objects.filter(item__legal_entity__tax_id=COMPANY_TAX_ID,item__kind='coupling',item__method='p1',item__archived=False,item__deleted_at__isnull=True,item__reserved_job__isnull=True,item__diameter_mm=job.section.diameter_mm,item__wall_mm__gte=job.wall_mm,item__length_mm__in=[1000,1500,2000,2500],quantity__gte=1).select_related('item','warehouse').prefetch_related('warehouse__served_objects','warehouse__served_brigades').order_by('item__sku','warehouse_id')
    if user is not None:rows=rows.filter(warehouse__in=stock.warehouses(user),item__in=stock.items(user))
    for b in rows:
        if b.item.coupling_id and hasattr(b.item.coupling,'installation'):continue
        if eligible(b.warehouse,job):groups[b.item.wall_mm][b.item.length_mm].append(b)
    result=[];lengths=[1000,1500,2000,2500]
    for wall,available in sorted(groups.items()):
        limits=[min(target//length,sum(int(b.quantity) for b in available[length])) for length in lengths]
        for counts in product(*(range(n+1) for n in limits)):
            if sum(n*length for n,length in zip(counts,lengths))!=target or sum(counts)<2:continue
            selected=[];parts=[]
            for count,length in zip(counts,lengths):
                parts.extend([length]*count)
                for balance in available[length]:
                    q=min(count,int(balance.quantity))
                    if q:selected.append({'item':str(balance.item_id),'version':balance.item.version,'warehouse':str(balance.warehouse_id),'quantity':q});count-=q
                    if not count:break
            payload={'job':str(job.pk),'version':job.version,'wall':str(wall),'parts':parts,'selected':selected}
            label=' + '.join(format(Decimal(n)/1000,'g').replace('.',',') for n in parts)+' м · стенка '+str(wall)+' мм'
            result.append({'label':label,'token':signing.dumps(payload,salt=SALT,compress=True),'count':len(parts),'rare':counts[3]})
    return sorted(result,key=lambda row:(row['count'],row['rare'],row['label']))[:8]

@transaction.atomic
def apply(user,job_id,token):
    actor=stock.manager(user);stock.lock()
    job=visible_jobs(actor).select_for_update(of=('self',)).get(pk=job_id)
    try:data=signing.loads(token,salt=SALT,max_age=3600)
    except signing.BadSignature:raise ValidationError('Вариант устарел. Откройте подбор заново.')
    available=[signing.loads(c['token'],salt=SALT) for c in choices(job,actor)]
    if data not in available:raise ValidationError('Данные или остатки изменились. Подберите состав заново.')
    before=job.composition;job.composition='+'.join(format(Decimal(n)/1000,'g') for n in data['parts']);job.version+=1;job.save(update_fields=['composition','version'])
    for selected in data['selected']:
        for _ in range(selected['quantity']):
            item=stock.items(actor).get(pk=selected['item'])
            if not item.individual and not item.coupling_id:item=stock_planning.split(actor,item.pk,item.version,selected['warehouse'])
            stock_planning.reserve(actor,item.pk,item.version,job.pk)
    audit(actor,'repair_prepared',entity_id=str(job.pk),kind='p1_composition',previous=before,composition=job.composition,wall=data['wall'])
    return job
