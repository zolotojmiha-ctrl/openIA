"""Scoped customer demand snapshots and durable, per-recipient mail jobs."""
import hashlib,json,uuid
from collections import defaultdict
from datetime import datetime,timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.utils import timezone
from accounts.models import User
from accounts.permissions import permitted
from repairs.access import visible_jobs
from postoffice.access import boxes
from postoffice.services import addresses
from postoffice.models import Letter,Attachment
from .models import DemandMailing,DemandMailRun,DemandMailDelivery
from .diameters import label,OD_TO_DN

SUBJECT='Потребность в ремонтных конструкциях для ООО "Армада-Сибирь"'
def manager(user):
    if not user.is_active or user.role=='customer' or not permitted(user,'stock.manage'):raise PermissionDenied
    return user

def scopes(config,user):
    manager(user)
    from .access import visible_objects,visible_contracts
    from repairs.access import visible_sections
    if config.project_objects.exclude(pk__in=visible_objects(user)).exists() or config.contracts.exclude(pk__in=visible_contracts(user)).exists() or config.sections.exclude(pk__in=visible_sections(user)).exists():
        raise PermissionDenied('Доступ к объектам рассылки изменился.')
    ids=list(config.project_objects.values_list('pk',flat=True))
    if not ids:raise ValidationError('Выберите объекты рассылки.')
    if config.sections.exclude(project_object_id__in=ids).exists():raise ValidationError('Участки не входят в выбранные объекты.')
    jobs=visible_jobs(user).filter(section__project_object_id__in=ids)
    if config.sections.exists():jobs=jobs.filter(section__in=config.sections.all())
    if config.contracts.exists():jobs=jobs.filter(contract__in=config.contracts.all())
    return jobs

def snapshot(config,user,now=None):
    from .repair_materials import all_demand
    allowed=set(scopes(config,user).values_list('pk',flat=True))
    from .demand_details import location,order
    grouped={};details=[];issues=[]
    for record in all_demand(user):
        job=record['job']
        if job.pk not in allowed:continue
        if record['coupling_issues']:
            raise ValidationError('Потребность неполная: уточните тип, диаметр, длину или стенку оставшихся конструкций.')
        for row in record['couplings']:
            if not row['missing']:continue
            if row['diameter'] not in OD_TO_DN:raise ValidationError('Уточните номинальный диаметр оставшихся конструкций перед отправкой заказчику.')
            key=(str(job.section.project_object_id),str(job.section_id),row['method'],row['diameter'],row['length'],str(row['wall']))
            line=grouped.setdefault(key,{'object':job.section.project_object.name,'section':job.section.name,
                'object_id':key[0],'section_id':key[1],'method':job.get_method_display(),
                'dn':label(row['diameter']),'length':row['length'],'wall':str(row['wall']),'quantity':0})
            line['quantity']+=int(row['missing'])
            details.append({**line,**location(job),'quantity':int(row['missing'])})
    rows=sorted(grouped.values(),key=lambda r:(r['object'],r['section'],r['object_id'],r['section_id'],r['method'],r['dn'],r['length'],r['wall']))
    for row in rows:
        row['placements']=sorted([d for d in details if all(d[k]==row[k] for k in ('object_id','section_id','method','dn','length','wall'))],key=order)
    recipients=list(dict.fromkeys(addresses(config.recipients)))
    if not recipients:raise ValidationError('Укажите получателей.')
    details.sort(key=lambda r:(r['object'],r['section'],r['object_id'],r['section_id'],order(r),r['method'],r['length'],r['wall']))
    data={'basis':'shortage-v3','rows':rows,'details':details,'recipients':recipients,'sender':config.mailbox.address,'version':config.version}
    digest=hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return dict(data,digest=digest,date=(now or timezone.now()).astimezone(ZoneInfo(config.timezone)).strftime('%d.%m.%Y'))

def body(data):
    text=['Направляем потребность в ремонтных конструкциях для ООО «Армада-Сибирь» по состоянию на '+data['date']+'.','']
    scope=None
    for row in data['rows']:
        key=(row['object_id'],row['section_id'])
        if key!=scope:
            text.extend(['',row['object']+' · '+row['section']]);scope=key
        text.append(f"{row['method']} · {row['dn']} · длина {row['length']} мм · стенка {row['wall']} мм — {row['quantity']} шт.")
    if not data['rows']:text.append('Недостающих муфт нет.')
    text+=['','Детальная ведомость приложена к письму.','','Письмо сформировано автоматически. Пожалуйста, не отвечайте на него. По вопросам изготовления и поставки свяжитесь с ответственным представителем ООО «Армада-Сибирь».','www.armada-sibir.ru','info@armada-sibir.ru','+7 (922) 114 20 00']
    return '\n'.join(text)

@transaction.atomic
def enqueue(user,pk,token,expected_digest=None,automatic=False,week=None):
    actor=User.objects.select_for_update(no_key=True).get(pk=user.pk)
    if actor.session_version!=user.session_version:raise PermissionDenied
    manager(actor)
    config=DemandMailing.objects.select_for_update().select_related('mailbox').get(pk=pk,owner=actor)
    existing=DemandMailRun.objects.filter(pk=token).first()
    if existing:
        if existing.mailing_id!=pk or existing.author_id!=actor.pk:raise PermissionDenied
        return existing
    if automatic:
        if not config.enabled:return None
        if DemandMailRun.objects.filter(mailing=config,automatic=True,week=week).exists():return None
    if not boxes(actor).filter(pk=config.mailbox_id,state='active').exists():raise ValidationError('Подключите доступный ящик отправителя в разделе «Почта».')
    data=snapshot(config,actor)
    if expected_digest and data['digest']!=expected_digest:raise ValidationError('Потребность или настройки изменились. Сформируйте предпросмотр заново.')
    from .demand_mail_xlsx import workbook
    payload=workbook(data)
    if len(payload)>10*1024*1024:raise ValidationError('Слишком большая ведомость. Разделите рассылку по объектам.')
    DemandMailing.objects.filter(pk=config.pk).update(last_error='')
    run=DemandMailRun.objects.create(pk=token,mailing=config,author=actor,version=config.version,automatic=automatic,week=week,snapshot=data)
    for recipient in data['recipients']:
        lid=uuid.uuid4()
        letter=Letter.objects.create(pk=lid,mailbox=config.mailbox,author=actor,to=recipient,subject=SUBJECT,
            body=body(data),state='queued',message_id=f'<{lid}@armada-sibir.ru>')
        Attachment.objects.create(letter=letter,name='Потребность-'+data['date']+'.xlsx',data=payload)
        DemandMailDelivery.objects.create(run=run,letter=letter)
    return run

def authorize_letter(letter):
    delivery=DemandMailDelivery.objects.filter(letter=letter).select_related('run__mailing','run__author').first()
    if not delivery:return ''
    if delivery.run.snapshot.get('basis')!='shortage-v3':return 'Потребность сформирована по прежним правилам. Сформируйте её заново.'
    config=delivery.run.mailing
    if config.version!=delivery.run.version or delivery.run.automatic and not config.enabled:return 'Рассылка выключена или её настройки изменились.'
    try:scopes(config,delivery.run.author)
    except (PermissionDenied,ValidationError):return 'Доступ к данным рассылки изменился.'
    return ''

def queue_due(now=None):
    now=now or timezone.now()
    for pk in DemandMailing.objects.filter(enabled=True).values_list('pk',flat=True):
        try:
            config=DemandMailing.objects.select_related('owner').get(pk=pk)
            local=now.astimezone(ZoneInfo(config.timezone))
            day=local.date()-timedelta(days=(local.weekday()-config.weekday)%7)
            due=datetime.combine(day,config.send_time,tzinfo=ZoneInfo(config.timezone))
            if due>local or due<config.not_before:continue
            enqueue(config.owner,config.pk,uuid.uuid4(),automatic=True,week=day-timedelta(days=day.weekday()))
        except (PermissionDenied,ValidationError) as exc:
            DemandMailing.objects.filter(pk=pk).update(last_error=('; '.join(exc.messages) if isinstance(exc,ValidationError) else 'Доступ к данным или отправителю изменился.')[:300])
