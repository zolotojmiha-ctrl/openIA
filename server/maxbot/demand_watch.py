"""Notify on observed shortage changes; no stock movement or mail sending."""
import hashlib,json
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from accounts.permissions import permitted
from projects.repair_materials import all_demand
from repairs.access import visible_jobs
from .models import Binding,DemandWatch,Outbox
from .links import current
from .leadership import supported,contract_name,access_signature,link


def snapshot(user):
    allowed=set(visible_jobs(user).values_list('pk',flat=True));rows=[]
    for record in all_demand(user):
        job=record['job']
        if job.pk not in allowed:continue
        for line in record['couplings']:
            if line['missing']:
                rows.append({'job':str(job.pk),'contract':contract_name(user,job.contract),'object':job.section.project_object.name,
                    'section':job.section.name,'method':line['method'],'length':line['length'],'wall':str(line['wall']),'quantity':line['missing']})
    rows.sort(key=lambda r:(r['job'],r['method'],r['length'],r['wall']))
    digest=hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return digest,rows


def queue_changes():
    if not settings.ARMADA_MAX_ENABLED:return
    for binding in Binding.objects.filter(active=True).filter(Q(user__leader_slot__in=[1,2])|Q(user__role='pto')).select_related('user'):
        if not current(binding) or not supported(binding.user) or not permitted(binding.user,'stock.manage'):continue
        digest,rows=snapshot(binding.user)
        with transaction.atomic():
            fresh=Binding.objects.select_for_update(of=('self',),no_key=True).select_related('user').get(pk=binding.pk)
            if not current(fresh) or access_signature(fresh.user)!=access_signature(binding.user):continue
            watch,_=DemandWatch.objects.select_for_update().get_or_create(binding=fresh)
            if watch.digest==digest:continue
            initial=not watch.digest;watch.digest=digest;watch.version+=1;watch.save()
            if initial and not rows:continue
            body='Текущая нехватка муфт' if initial else 'Потребность в муфтах изменилась'
            if not rows:body+='\nНедостающих позиций нет.'
            for row in rows[:6]:body+=f'\n\nКонтракт: {row["contract"]}\n{row["object"]} · {row["section"]}\n{row["method"].upper()} · {row["length"]} мм · стенка {row["wall"]} мм: {row["quantity"]} шт.'
            Outbox.objects.get_or_create(key=f'shortage:{fresh.pk}:{watch.version}',defaults={'binding':fresh,'max_user':fresh.max_user,'chat':fresh.chat,
                'body':{'text':body[:3500],'attachments':[{'type':'inline_keyboard','payload':{'buttons':[[link('Открыть подробную потребность','stock_coverage')]]}}]},
                'scope':{'leader':True,'demand_digest':digest,'access_signature':access_signature(fresh.user)},
                'available_at':timezone.now(),'expires':timezone.now()+timedelta(days=1)})
