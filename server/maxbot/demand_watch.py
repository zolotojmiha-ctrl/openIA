"""Notify on observed shortage changes; no stock movement or mail sending."""
import hashlib,json
from decimal import Decimal
from .labels import location
from projects.diameters import label as diameter_label
from datetime import timedelta
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from accounts.permissions import permitted
from projects.repair_materials import all_demand
from repairs.access import visible_jobs
from repairs.models import METHODS
from .models import Binding,DemandWatch,Outbox
from .links import current
from .leadership import supported,contract_name,access_signature,link


def snapshot(user):
    allowed=set(visible_jobs(user).values_list('pk',flat=True));rows=[]
    for record in all_demand(user):
        if record.get('allocation_failed'):
            raise ValidationError('Совместный подбор не завершён.')
        job=record['job']
        if job.pk not in allowed:continue
        for line in record['couplings']:
            if line['missing']:
                rows.append({'job':str(job.pk),'contract':contract_name(user,job.contract),'object':job.section.project_object.name,
                    'section':job.section.name,'contract_id':str(job.contract_id or ''),'object_id':str(job.section.project_object_id),'section_id':str(job.section_id),
                    'diameter':job.section.diameter_mm,'method':line['method'],'length':line['length'],'wall':format(Decimal(str(line['wall'])).normalize(),'f') if line['wall'] is not None else '','quantity':line['missing']})
    rows.sort(key=lambda r:(r['job'],r['method'],r['length'],r['wall']))
    facts=[{k:v for k,v in row.items() if k not in ('contract','object','section')} for row in rows]
    digest=hashlib.sha256(json.dumps(facts,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return digest,rows


def queue_changes():
    if not settings.ARMADA_MAX_ENABLED:return
    for binding in Binding.objects.filter(active=True).filter(Q(user__leader_slot__in=[1,2])|Q(user__role='pto')).select_related('user'):
        if not current(binding) or not supported(binding.user) or not permitted(binding.user,'stock.manage'):continue
        try:digest,rows=snapshot(binding.user)
        except ValidationError:continue
        with transaction.atomic():
            fresh=Binding.objects.select_for_update(of=('self',),no_key=True).select_related('user').get(pk=binding.pk)
            if not current(fresh) or access_signature(fresh.user)!=access_signature(binding.user):continue
            watch,_=DemandWatch.objects.select_for_update().get_or_create(binding=fresh)
            if watch.format_version<2:
                watch.digest=digest;watch.format_version=2;watch.save(update_fields=['digest','format_version'])
                continue  # Establish the new comparison baseline without a fake change notice.
            if watch.digest==digest:continue
            initial=not watch.digest;watch.digest=digest;watch.version+=1;watch.save()
            if initial and not rows:continue
            body='Текущая нехватка муфт' if initial else 'Потребность в муфтах изменилась'
            if not rows:body+='\nНедостающих позиций нет.'
            grouped={}
            for row in rows:
                key=tuple(row.get(k,row.get(k.replace('_id',''))) for k in ('contract_id','object_id','section_id','diameter','method','length','wall'))
                item=grouped.setdefault(key,dict(row,quantity=0));item['quantity']+=row['quantity']
            for row in list(grouped.values())[:6]:
                dn=diameter_label(row['diameter'])+' · ' if row.get('diameter') else ''
                body+=f'\n\nКонтракт: {row["contract"]}\n{location(row["object"],row["section"])}\n{dict(METHODS).get(row["method"],row["method"])} · {dn}{row["length"]} мм · стенка {row["wall"]} мм: {row["quantity"]} шт.'
            if len(grouped)>6:body+=f'\nЕщё типоразмеров: {len(grouped)-6}. Полный список — в ЛК.'
            Outbox.objects.get_or_create(key=f'shortage:{fresh.pk}:{watch.version}',defaults={'binding':fresh,'max_user':fresh.max_user,'chat':fresh.chat,
                'body':{'text':body[:3500],'attachments':[{'type':'inline_keyboard','payload':{'buttons':[[link('Открыть подробную потребность','stock_coverage')]]}}]},
                'scope':{'leader':True,'demand_digest':digest,'access_signature':access_signature(fresh.user)},
                'available_at':timezone.now(),'expires':timezone.now()+timedelta(days=1)})
