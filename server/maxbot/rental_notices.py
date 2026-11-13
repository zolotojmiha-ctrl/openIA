"""Rental ledger notifications and missing-day escalation; no automatic hour entries."""
from datetime import datetime,time,timedelta,date
from django.conf import settings
from django.db import transaction
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from accounts.models import User,AuditEvent
from projects import rentals
from .models import Binding,Outbox
from .links import current
from .rental_reminders import OMSK,available,pending,payload


def leaders():
    return Binding.objects.filter(active=True,user__is_active=True,user__leader_slot__in=[1,2]).select_related('user')


def event_data(user,event):
    if not user.is_active or event.action!='equipment_assigned':return None
    kind=event.details.get('kind')
    if kind in ('rental_dispute','rental_dispute_resolved'):
        from projects.models import RentalDispute
        try:row=RentalDispute.objects.select_related('access__rental__placement__equipment','access__rental__placement__project_object','access__user').filter(pk=event.details.get('dispute')).first()
        except (ValueError,TypeError):return None
        if not row:return None
        rental=row.access.rental
        if user.is_leader:
            if not rentals.visible(user).filter(pk=rental.pk).exists():return None
        else:
            from projects.rental_owner import visible
            if kind!='rental_dispute_resolved' or row.access.user_id!=user.pk or not visible(user).filter(pk=row.access_id).exists():return None
        if kind=='rental_dispute_resolved' and not row.resolved_at:return None
        text=('Разногласие по табелю' if kind=='rental_dispute' else 'Разногласие рассмотрено')+f'\n{rental.placement.equipment}\n{row.date:%d.%m.%Y}\n'+(row.text if kind=='rental_dispute' else row.resolution)
        from types import SimpleNamespace
        return rental,SimpleNamespace(date=row.date),text
    if kind!='rental_day':return None
    if not user.is_leader and user.role!='lessor':return None
    try:
        if user.is_leader:rental=rentals.visible(user).filter(pk=event.details['rental']).first()
        else:
            from projects.rental_owner import visible
            access=visible(user).filter(rental_id=event.details['rental']).first()
            rental=access.rental if access else None
        day=rental.days.filter(date=date.fromisoformat(event.details['date']),version=event.details['version']).select_related('updated_by').first() if rental else None
    except (ValueError,TypeError,KeyError,ValidationError):return None
    if not day:return None
    name=day.updated_by.full_name+': ' if user.is_leader else ''
    text=f'{name}Наработка за {day.date:%d.%m.%Y}\n{rental.placement.equipment}\n{rental.placement.project_object.name}\n{day.get_kind_display()}'
    if event.details.get('previous_kind'):
        text+='\nИсправление: '+str(event.details.get('previous_hours'))+' → '+str(day.hours)+' ч.\nПричина: '+event.details.get('reason','')
    return rental,day,text


def queue_event(event):
    if not settings.ARMADA_MAX_ENABLED:return
    recipients=Binding.objects.filter(active=True,user__is_active=True).filter(Q(user__leader_slot__in=[1,2])|Q(user__role='lessor',user__rental_accesses__rental_id=event.details.get('rental'),user__rental_accesses__active=True)).select_related('user').distinct()
    for binding in recipients:
        if not current(binding):continue
        data=event_data(binding.user,event)
        if not data:continue
        rental,day,text=data;body=payload(rental,day.date);body['text']=text[:3500]
        body['attachments'][0]['payload']['buttons'][0][0]['text']='Открыть табель'
        if binding.user.role=='lessor':
            from .leadership import link
            body['attachments'][0]['payload']['buttons'][0][0]=link('Открыть табель','lessor_detail',[rental.pk])
        Outbox.objects.get_or_create(key=f'rental-event:{event.pk}:{binding.pk}',defaults={
            'binding':binding,'max_user':binding.max_user,'chat':binding.chat,'body':body,
            'scope':{'rental_event':event.pk},'available_at':timezone.now(),'expires':timezone.now()+timedelta(days=1)})


def queue_overdue(now):
    local=now.astimezone(OMSK)
    if local.hour<21:return 0
    day=local.date();count=0
    for master in User.objects.filter(role='foreman',is_active=True,leader_slot__isnull=True):
        for rental in available(master,day):
            if not pending(master,rental,day):continue
            for binding in leaders():
                if not current(binding) or not rentals.visible(binding.user).filter(pk=rental.pk).exists():continue
                body=payload(rental,day);body['text']=f'Не заполнена наработка за {day:%d.%m.%Y}\nОтветственный: {master.full_name}\n{rental.placement.equipment}\n{rental.placement.project_object.name}\nОтметки за день пока нет.'
                body['attachments'][0]['payload']['buttons'][0][0]['text']='Открыть табель'
                row,created=Outbox.objects.get_or_create(key=f'rental-overdue:{binding.user_id}:{master.pk}:{rental.pk}:{day}',defaults={
                    'binding':binding,'max_user':binding.max_user,'chat':binding.chat,'body':body,
                    'scope':{'rental_overdue':{'rental':str(rental.pk),'master':str(master.pk),'date':str(day)}},'available_at':now,
                    'expires':datetime.combine(day+timedelta(days=1),time.min,tzinfo=OMSK)})
                if not created and row.binding_id!=binding.pk:
                    with transaction.atomic():
                        row=Outbox.objects.select_for_update().get(pk=row.pk)
                        if row.binding_id!=binding.pk and (row.state=='pending' or row.state=='cancelled' and row.attempts==0):
                            row.binding=binding;row.max_user=binding.max_user;row.chat=binding.chat;row.state='pending';row.body=body
                            row.save(update_fields=['binding','max_user','chat','state','body']);created=True
                count+=created
    return count


def delivery_allowed(user,scope):
    if scope.get('rental_event'):
        event=AuditEvent.objects.filter(pk=scope['rental_event']).first()
        return bool(event and event_data(user,event))
    data=scope.get('rental_overdue')
    if not user.is_leader or not user.is_active or not isinstance(data,dict):return False
    try:
        local=timezone.now().astimezone(OMSK);day=date.fromisoformat(data['date'])
        master=User.objects.filter(pk=data['master'],role='foreman',is_active=True).first()
        rental=rentals.visible(user).filter(pk=data['rental']).first()
    except (ValueError,TypeError,KeyError,ValidationError):return False
    return bool(day==local.date() and local.hour>=21 and master and rental and pending(master,rental,day))
