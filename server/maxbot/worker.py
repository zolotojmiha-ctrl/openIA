"""Crash-safe intake, report transactions and conservative message delivery."""
from datetime import timedelta
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from accounts.models import User
from documents.storage import private_path
from repairs.access import reportable_jobs
from . import dialog, links, transport
from .models import Binding, Inbox, Outbox, Draft


def discard(keys):
    for key in keys:
        private_path(key).unlink(missing_ok=True)


def process(key):
    stored = []
    try:
        with transaction.atomic():
            # All employee work uses the same user -> binding -> event lock order.
            original = Inbox.objects.get(pk=key)
            active = Binding.objects.filter(max_user=original.max_user, chat=original.chat, active=True).first()
            binding=None
            if active:
                # Serialize actions without blocking foreign-key checks on the
                # actor from a concurrent LC repair update. Lock this exact
                # binding after its user, never a newly appeared replacement.
                User.objects.select_for_update(no_key=True).get(pk=active.user_id)
                binding = Binding.objects.select_for_update(no_key=True,of=('self',)).select_related('user').filter(
                    pk=active.pk,max_user=original.max_user,chat=original.chat,active=True).first()
            event = Inbox.objects.select_for_update().get(pk=key)
            if event.state != 'pending':
                return
            if event.payload.get('callback_id'):
                # Separate durable acknowledgement, without report data or names.
                Outbox.objects.get_or_create(key=f'in:{event.pk}:ack', defaults={
                    'binding': binding if binding and links.current(binding) else None,
                    'max_user': event.max_user, 'chat': event.chat,
                    'body': {'_callback_id': event.payload['callback_id'], 'notification': 'Запрос обработан.'},
                    'available_at': timezone.now(), 'expires': timezone.now() + timedelta(seconds=30)})
            try:
                with transaction.atomic():
                    timestamp = event.payload.get('timestamp')
                    if binding and links.current(binding) and timestamp is not None:
                        if timestamp < binding.last_timestamp:
                            raise ValidationError('Сообщение пришло с задержкой. Ответьте на текущий вопрос заново; прежний ответ не применён.')
                        binding.last_timestamp = timestamp
                        binding.save(update_fields=['last_timestamp'])
                    if event.payload.get('link_digest'):
                        links.request_link(event.payload['link_digest'], event.max_user, event.chat)
                        dialog.queue(event, 'Запрос принят. Вернитесь в свой личный кабинет и подтвердите подключение MAX.')
                    elif binding and links.current(binding):
                        dialog.handle(event, binding, stored)
                    else:
                        dialog.queue(event, 'Подключите MAX в своём профиле «Армада Управление» и подтвердите привязку в ЛК.')
            except (ValidationError, PermissionDenied, ValueError, KeyError, IndexError, TypeError) as exc:
                discard(stored)
                stored.clear()
                # No DB/raw transport errors, signed URLs or credentials in outgoing messages.
                text = '\n'.join(exc.messages) if isinstance(exc, ValidationError) else 'Действие недоступно или устарело. Откройте /меню и выберите действие заново.'
                valid = binding if binding and links.current(binding) else None
                draft = Draft.objects.filter(binding=valid, state__in=['editing', 'photos', 'confirm']).order_by('-created_at').first() if valid else None
                buttons = [dialog.button('Вернуться к фотоотчёту','/фото') if draft.action=='progress' else dialog.button('Исправить черновик',f'edit:{draft.pk}')] if draft else []
                if not valid:
                    text = 'Привязка не выполнена или доступ изменился. Проверьте профиль в ЛК.'
                if draft and draft.action=='ddk':
                    if not isinstance(exc,ValidationError):text='Действие ДДК недоступно или устарело. Откройте актуальный список: /ддк. При изменении прав обратитесь к руководителю.'
                    buttons=[dialog.button('Вернуться к ДДК',f'ddk:resume:{draft.pk}'),dialog.button('Исправить ответы',f'ddk:edit:{draft.pk}'),dialog.button('Отменить','/отмена')]
                dialog.queue(event, text[:3500], buttons, binding=valid, job=draft.job if draft else None, suffix='error')
            event.state = 'done'
            event.payload = {}  # Minimise retention of message text and signed CDN URLs.
            event.processed_at = timezone.now()
            event.save(update_fields=['state', 'payload', 'processed_at'])
    except Exception:
        discard(stored)
        # Keep the event for inspection/recovery, but never leak event payload to logs.
        Inbox.objects.filter(pk=key, state='pending').update(state='failed')
        raise


def delivery_allowed(row):
    if not row.binding_id:
        if row.body.get('_callback_id') and row.body.get('notification') == 'Запрос обработан.':
            return True
        return row.body.get('text') in (
            'Запрос принят. Вернитесь в свой личный кабинет и подтвердите подключение MAX.',
            'Подключите MAX в своём профиле «Армада Управление» и подтвердите привязку в ЛК.',
            'Привязка не выполнена или доступ изменился. Проверьте профиль в ЛК.')
    binding = Binding.objects.select_related('user').get(pk=row.binding_id)
    if not links.current(binding) or binding.max_user != row.max_user or binding.chat != row.chat:
        return False
    if row.scope.get('work_navigation') is not None:
        from .work_navigation import delivery_allowed as navigation_allowed
        return navigation_allowed(binding,row.scope['work_navigation'])
    if row.scope.get('layout_work_sections') is not None:
        from .layouts import current_jobs
        ids=row.scope['layout_work_sections']
        return set(ids)=={str(pk) for pk in current_jobs(binding.user).filter(section_id__in=ids).values_list('section_id',flat=True)}
    if row.scope.get('layout_jobs') is not None:
        from .layouts import current_jobs
        from repairs.access import visible_sections
        ids=row.scope['layout_jobs']
        rows=current_jobs(binding.user).filter(pk__in=ids)
        if row.scope.get('layout_selected_section'):
            section_id=row.scope['layout_selected_section']
            if not visible_sections(binding.user).filter(pk=section_id).exists():return False
            rows=rows.filter(section_id=section_id)
        return set(ids)=={str(pk) for pk in rows.values_list('pk',flat=True)}
    if row.scope.get('pipe_layout'):
        from .layouts import delivery_allowed as layout_allowed
        return layout_allowed(binding,row.scope)
    if row.scope.get('archive'):
        from .archive import delivery_allowed as archive_allowed
        return archive_allowed(binding,row.scope)
    if row.scope.get('ddk_draft'):
        from .ddk import delivery_allowed as ddk_allowed
        return ddk_allowed(binding,row.scope)
    if row.scope.get('rental_event') or row.scope.get('rental_overdue'):
        from .rental_notices import delivery_allowed as rental_notice_allowed
        return rental_notice_allowed(binding.user,row.scope)
    if row.scope.get('rental_reminder'):
        from .rental_reminders import delivery_allowed as rental_allowed
        return rental_allowed(binding.user,row.scope)
    if row.scope.get('lessor'):
        from .lessor import delivery_allowed as lessor_allowed
        return lessor_allowed(binding.user,row.scope)
    if row.scope.get('mechanic'):
        from .mechanic import delivery_allowed
        return delivery_allowed(binding.user,row.scope)
    if row.scope.get('leader'):
        from .leadership import delivery_allowed
        return delivery_allowed(binding.user, row.scope)
    jobs = reportable_jobs(binding.user)
    if row.job_id and not jobs.filter(pk=row.job_id).exists():
        return False
    if row.scope.get('revision'):
        from repairs.models import Report
        if not jobs.filter(pk=row.job_id, state='progress').exclude(review_note='').exists():
            return False
        latest = Report.objects.filter(job_id=row.job_id, kind='revision').order_by('-created_at').first()
        if not latest or str(latest.pk) != row.scope['revision']:
            return False
    if row.scope.get('jobs') and set(row.scope['jobs']) != {str(pk) for pk in jobs.filter(pk__in=row.scope['jobs']).values_list('pk', flat=True)}:
        return False
    if row.scope.get('sections') and set(row.scope['sections']) != {str(pk) for pk in jobs.filter(section_id__in=row.scope['sections']).values_list('section_id', flat=True)}:
        return False
    return True


def deliver(pk):
    if not settings.ARMADA_MAX_ENABLED:
        return
    with transaction.atomic():
        row = Outbox.objects.select_for_update().get(pk=pk)
        if row.state != 'pending' or row.available_at > timezone.now():
            return
        if row.expires <= timezone.now() or not delivery_allowed(row):
            row.state = 'cancelled'
            row.save(update_fields=['state'])
            return
        if Outbox.objects.filter(chat=row.chat, state__in=['pending', 'sending'], expires__gt=timezone.now()).filter(
                Q(created_at__lt=row.created_at) | Q(created_at=row.created_at, pk__lt=row.pk)).exists():
            return
        # Commit the sending claim BEFORE network I/O. A crash will be unknown,
        # never automatic redelivery of a potentially accepted external message.
        row.state = 'sending'
        row.attempts += 1
        row.available_at = timezone.now()
        row.save(update_fields=['state', 'attempts', 'available_at'])
    delay = min(300, 30 * 2**row.attempts)
    try:
        # Check access again immediately before sending, including after a claim.
        row.refresh_from_db()
        if not delivery_allowed(row) or not settings.ARMADA_MAX_ENABLED:
            state = 'cancelled'
        else:
            transport.send(row.chat, row.body)
            state = 'sent'
    except transport.DeliveryError as exc:
        state = exc.state
        if exc.retry_after is not None:
            delay = max(delay, exc.retry_after)
        if state == 'pending' and row.attempts >= 5:
            state = 'failed'
    Outbox.objects.filter(pk=pk, state='sending').update(state=state,
        available_at=timezone.now() + timedelta(seconds=delay))


def tick(deliver_messages=False, limit=30):
    if not settings.ARMADA_MAX_ENABLED:
        return 0
    from .rental_reminders import maybe_queue_due
    maybe_queue_due()
    count = 0
    for key in Inbox.objects.filter(state='pending').order_by('created_at', 'key').values_list('pk', flat=True)[:limit]:
        process(key)
        count += 1
    Outbox.objects.filter(state='sending', available_at__lt=timezone.now()-timedelta(minutes=2)).update(state='unknown')
    if deliver_messages:
        for pk in Outbox.objects.filter(state='pending', available_at__lte=timezone.now()).order_by('created_at', 'pk').values_list('pk', flat=True)[:limit]:
            deliver(pk)
    return count
