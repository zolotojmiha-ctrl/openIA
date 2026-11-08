from datetime import timedelta
from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from django.urls import reverse
from repairs.models import Report
from repairs.access import reportable_jobs
from .links import current
from .models import Binding, Outbox


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
