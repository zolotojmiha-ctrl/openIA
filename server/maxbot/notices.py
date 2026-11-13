from datetime import timedelta
from django.conf import settings
from django.db.models import Q
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from django.urls import reverse
from repairs.models import Report
from repairs.access import reportable_jobs
from .links import current
from .models import Binding, Outbox
from accounts.models import AuditEvent


@receiver(post_save, sender=AuditEvent, dispatch_uid='max_leader_notice')
def leader_notice(sender, instance, created, **kwargs):
    if created and instance.action=='equipment_assigned' and instance.details.get('kind') in ('rental_day','rental_dispute','rental_dispute_resolved'):
        from .rental_notices import queue_event
        queue_event(instance)
    if (not settings.ARMADA_MAX_ENABLED or not created or
            instance.action not in ('repair_reported', 'equipment_updated', 'repair_prepared','equipment_issue')):
        return
    from .leadership import subject
    for binding in Binding.objects.select_related('user').filter(active=True).filter(Q(user__leader_slot__in=[1,2])|Q(user__role__in=['pto','mechanic'])):
        if not current(binding):
            continue
        mechanic = binding.user.role=='mechanic' and not binding.user.is_leader
        if mechanic:
            from .mechanic import subject as mechanic_subject
            notice = mechanic_subject(binding.user,instance)
        else:notice = subject(binding.user, instance)
        if not notice:
            continue
        text, target = notice
        Outbox.objects.get_or_create(key=f'{"mechanic" if mechanic else "leader"}:{instance.pk}:{binding.pk}', defaults={
            'binding': binding, 'max_user': binding.max_user, 'chat': binding.chat,
            'scope': {'mechanic':True,'mechanic_event':instance.pk} if mechanic else {'leader': True, 'leader_event': instance.pk},
            'body': {'text': text[:3500], 'attachments': [{'type': 'inline_keyboard', 'payload': {'buttons': [[target]]}}]},
            'available_at': timezone.now(), 'expires': timezone.now() + timedelta(days=1)})


@receiver(post_save, sender=Report, dispatch_uid='max_revision_notice')
def revision_notice(sender, instance, created, **kwargs):
    if not settings.ARMADA_MAX_ENABLED or not created or instance.kind != 'revision':
        return
    job = instance.job
    binding = Binding.objects.select_related('user').filter(user_id=job.assigned_to_id, active=True).first()
    if not binding or not current(binding) or not reportable_jobs(binding.user).filter(pk=job.pk).exists():
        return
    # Decision and outbox commit together. No external I/O in the leader's request.
    Outbox.objects.get_or_create(key='revision:' + str(instance.pk), defaults={
        'binding': binding, 'job': job, 'max_user': binding.max_user, 'chat': binding.chat,
        'scope': {'revision': str(instance.pk)},
        'body': {'text': 'Отчёт возвращён на доработку. Причина и замечания доступны в карточке ремонта в ЛК.',
                 'attachments': [{'type': 'inline_keyboard', 'payload': {'buttons': [[{
                     'type': 'link', 'text': 'Открыть ремонт в ЛК',
                     'url': settings.ARMADA_MAX_PUBLIC_ORIGIN.rstrip('/') + reverse('repair_detail', args=[job.pk])
                 }]]}}]},
        'available_at': timezone.now(), 'expires': timezone.now() + timedelta(days=1)})
