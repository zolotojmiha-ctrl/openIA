import hashlib
import secrets
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from accounts.models import User
from accounts.views import audit
from .models import Binding, LinkTicket, Draft, Outbox


def digest(code):
    return hashlib.sha256(code.encode()).hexdigest()


def supported(user):
    return user.is_leader or user.role in ('foreman','pto','mechanic')


def needs_onboarding(user):
    from django.conf import settings
    return (settings.ARMADA_MAX_ENABLED and user.is_active and supported(user)
            and not user.must_change_password
            and not Binding.objects.filter(user=user,active=True,session_version=user.session_version).exists())


def employee(user):
    fresh = User.objects.select_for_update().get(pk=user.pk)
    if (not fresh.is_active or not supported(fresh) or fresh.must_change_password
            or fresh.session_version != user.session_version):
        raise PermissionDenied('Доступ изменился. Войдите в ЛК заново.')
    return fresh


def current(binding):
    user = binding.user
    return (binding.active and user.is_active and supported(user)
            and not user.must_change_password and user.session_version == binding.session_version)


@transaction.atomic
def issue(user):
    user = employee(user)
    LinkTicket.objects.filter(user=user, consumed=False).update(consumed=True)
    code = secrets.token_urlsafe(32)
    LinkTicket.objects.create(digest=digest(code), user=user, session_version=user.session_version,
                              expires=timezone.now() + timedelta(minutes=10))
    audit(user, 'employee_updated', kind='max_link_requested')
    return code


def request_link(code_digest, sender, chat):
    ticket = LinkTicket.objects.select_for_update().filter(digest=code_digest, consumed=False,
                                                         expires__gt=timezone.now(), max_user=None).first()
    if not ticket:
        raise ValidationError('Ссылка истекла или уже использована. Создайте новую в ЛК.')
    # An incoming event is only a request, never approval of access.
    ticket.max_user = sender
    ticket.chat = chat
    ticket.save(update_fields=['max_user', 'chat'])


def deactivate(user):
    bindings = Binding.objects.filter(user=user, active=True)
    Draft.objects.filter(binding__in=bindings).exclude(state='submitted').update(state='cancelled')
    Outbox.objects.filter(binding__in=bindings, state='pending').update(state='cancelled')
    bindings.update(active=False)


@transaction.atomic
def approve(user, key):
    user = employee(user)
    ticket = LinkTicket.objects.select_for_update().filter(digest=key, user=user, consumed=False,
                session_version=user.session_version, expires__gt=timezone.now(), max_user__isnull=False).first()
    if not ticket:
        raise ValidationError('Запрос привязки устарел. Создайте новую ссылку.')
    if Binding.objects.filter(max_user=ticket.max_user, active=True).exclude(user=user).exists():
        raise ValidationError('Этот аккаунт MAX уже связан с другим сотрудником.')
    deactivate(user)
    binding = Binding.objects.create(user=user, max_user=ticket.max_user, chat=ticket.chat,
                                     session_version=user.session_version)
    LinkTicket.objects.filter(user=user, consumed=False).update(consumed=True)
    audit(user, 'employee_updated', kind='max_link_approved', binding=str(binding.pk))
    from .dialog import button
    leader = user.is_leader or user.role=='pto'
    mechanic = user.role=='mechanic' and not leader
    Outbox.objects.create(key='binding:' + str(binding.pk), binding=binding,
        max_user=binding.max_user, chat=binding.chat,
        body={'text': ('MAX подключён. Откройте меню руководителя.' if leader else
                       'MAX подключён. Откройте меню механика.' if mechanic else
                       'MAX подключён к вашему личному кабинету. Выберите назначенную вам работу.'),
              'attachments': [{'type': 'inline_keyboard', 'payload': {'buttons': [[
                  button('Меню руководителя' if leader else 'Меню механика' if mechanic else 'Мои работы', '/menu' if leader or mechanic else '/работы')]]}}]},
        scope={'leader': True} if leader else {'mechanic': True} if mechanic else {},
        available_at=timezone.now(), expires=timezone.now() + timedelta(minutes=30))
    return binding


@transaction.atomic
def revoke(user):
    user = employee(user)
    deactivate(user)
    LinkTicket.objects.filter(user=user, consumed=False).update(consumed=True)
    audit(user, 'employee_updated', kind='max_link_revoked')
