"""Daily rental-hour reminders. Read checks and queueing never fill a rental day."""
import logging,time as clock
from datetime import datetime,time,timedelta,date
from zoneinfo import ZoneInfo
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from projects import rentals
from .models import Binding,Outbox
from .links import current

OMSK=ZoneInfo('Asia/Omsk')
HOUR=20
_last_scan=None
logger=logging.getLogger(__name__)


def available(user,day):
    rows=rentals.visible(user).filter(placement__start_date__lte=day).filter(Q(placement__end_date__isnull=True)|Q(placement__end_date__gte=day))
    return rows.exclude(placement__equipment__status='archived').exclude(placement__project_object__status='archived').exclude(placement__contract__status='archived')


def pending(user,rental,day):
    if not user.is_active or user.is_leader or user.role!='foreman':return False
    p=rental.placement
    if day<p.start_date or p.end_date and day>p.end_date:return False
    if p.equipment.status=='archived' or p.project_object.status=='archived' or p.contract_id and p.contract.status=='archived':return False
    return (rentals.allowed_on(user,rental,day) and not rental.days.filter(date=day).exists()
            and not rental.periods.filter(starts_on__lte=day,ends_on__gte=day,reopened_at__isnull=True).exists())


def delivery_allowed(user,scope,now=None):
    value=scope.get('rental_reminder')
    if not isinstance(value,dict):return False
    try:
        day=date.fromisoformat(value['date'])
        rental=available(user,day).filter(pk=value['rental']).first()
    except (ValueError,TypeError,KeyError,ValidationError):return False
    local=(now or timezone.now()).astimezone(OMSK)
    return bool(day==local.date() and local.hour>=HOUR and rental and pending(user,rental,day))


def key(user,rental,day):return f'rental-hours:{user.pk}:{rental.pk}:{day.isoformat()}'


def payload(rental,day):
    from urllib.parse import urlencode
    p=rental.placement;machine=p.equipment
    url=(settings.ARMADA_MAX_PUBLIC_ORIGIN.rstrip('/')+reverse('equipment_detail',args=[machine.pk])+'?'+
         urlencode({'rental':str(rental.pk),'date':day.isoformat()})+'#rental-hours')
    return {'text':f'Не отмечена наработка за {day:%d.%m.%Y}.\n{machine.name} · {machine.registration_number or machine.inventory_number}\n'
            f'{p.project_object.name}\nУкажите: работал — 10 ч; наш простой — 4 ч; не по нашей вине — 0 ч.',
            'attachments':[{'type':'inline_keyboard','payload':{'buttons':[[{'type':'link','text':'Отметить наработку','url':url}]]}}]}


def queue_due(now=None):
    if not settings.ARMADA_MAX_ENABLED:return 0
    now=now or timezone.now();local=now.astimezone(OMSK)
    if local.hour<HOUR:return 0
    day=local.date();expires=datetime.combine(day+timedelta(days=1),time.min,tzinfo=OMSK);queued=0
    for binding in Binding.objects.filter(active=True,user__role='foreman',user__is_active=True,user__leader_slot__isnull=True).select_related('user'):
        if not current(binding):continue
        for rental in available(binding.user,day):
            if not pending(binding.user,rental,day):continue
            scope={'rental_reminder':{'rental':str(rental.pk),'date':day.isoformat()}}
            defaults={'binding':binding,'max_user':binding.max_user,'chat':binding.chat,'body':payload(rental,day),
                      'scope':scope,'available_at':now,'expires':expires}
            with transaction.atomic():
                row,created=Outbox.objects.get_or_create(key=key(binding.user,rental,day),defaults=defaults)
                if created:queued+=1;continue
                row=Outbox.objects.select_for_update().get(pk=row.pk)
                # A new confirmed binding may replace an unsent old one. Never
                # requeue sent/sending/unknown messages, including after relinking.
                if row.binding_id!=binding.pk and (row.state=='pending' or row.state=='cancelled' and row.attempts==0):
                    for name,value in defaults.items():setattr(row,name,value)
                    row.state='pending';row.save(update_fields=[*defaults,'state']);queued+=1
    return queued


def maybe_queue_due():
    global _last_scan
    moment=clock.monotonic()
    if _last_scan is not None and moment-_last_scan<60:return
    _last_scan=moment
    try:queue_due()
    except Exception:
        # Scheduling must not prevent normal reports and message delivery.
        logger.warning('MAX rental reminder scheduling failed; retrying next minute.')
