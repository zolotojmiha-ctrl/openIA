import hmac
import json
import re
from urllib.parse import quote
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from . import links
from .models import Binding, LinkTicket, Inbox, RateBucket


def enabled():
    return settings.ARMADA_MAX_ENABLED


@login_required
def connection(request):
    if not links.supported(request.user):
        raise PermissionDenied
    link = None
    if request.method == 'POST':
        try:
            action = request.POST.get('action')
            if action == 'revoke':
                links.revoke(request.user)
                messages.success(request, 'MAX отключён.')
                return redirect('max_connection')
            if not enabled():
                raise ValidationError('Подключение MAX ещё не включено администратором.')
            if action == 'issue':
                if not re.fullmatch(r'[A-Za-z0-9_]{1,100}', settings.ARMADA_MAX_BOT_NAME):
                    raise ValidationError('Администратор ещё не настроил адрес бота.')
                code = links.issue(request.user)
                link = 'https://max.ru/' + settings.ARMADA_MAX_BOT_NAME + '?start=' + quote(code)
            elif action == 'approve':
                try:
                    links.approve(request.user, request.POST.get('ticket', ''))
                except IntegrityError:
                    raise ValidationError('Этот аккаунт MAX уже привязан. Обновите страницу.')
                messages.success(request, 'MAX подключён. Вернитесь в бот и откройте ' +
                                 ('«Меню руководителя».' if request.user.is_leader or request.user.role=='pto' else '«Мои работы».'))
                return redirect('max_connection')
            else:
                raise ValidationError('Неизвестное действие.')
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
    pending = LinkTicket.objects.filter(user=request.user, consumed=False, expires__gt=timezone.now(),
                      session_version=request.user.session_version, max_user__isnull=False)
    binding = Binding.objects.select_related('user').filter(user=request.user, active=True).first()
    return render(request, 'maxbot/connection.html', {'binding': binding if binding and links.current(binding) else None,
                  'pending': pending, 'max_link': link, 'max_enabled': enabled()})


@login_required
def onboarding(request):
    if (not links.needs_onboarding(request.user) or
            request.session.get('max_onboarding') != str(request.user.pk)):
        return redirect('dashboard')
    if request.method == 'POST':
        action = request.POST.get('action')
        if action in ('connect', 'later'):
            request.session.pop('max_onboarding', None)
            return redirect('max_connection' if action == 'connect' else 'dashboard')
    return render(request, 'maxbot/onboarding.html')


def normalize(data):
    if not isinstance(data, dict):
        raise ValueError
    kind = data.get('update_type')
    if kind == 'bot_started':
        timestamp = data.get('timestamp')
        sender = data['user']['user_id']
        chat = data['chat_id']
        code = data.get('payload') or ''
        if not isinstance(code, str) or len(code) > 128:
            raise ValueError
        payload = {'kind': kind, 'link_digest': links.digest(code) if code else ''}
        identity = json.dumps([kind, sender, chat, code, data.get('timestamp')])
    elif kind in ('message_created', 'message_callback'):
        message = data['message']
        if message['recipient']['chat_type'] != 'dialog':
            raise ValueError
        chat = message['recipient']['chat_id']
        if kind == 'message_created':
            timestamp = message.get('timestamp')
            sender = message['sender']['user_id']
            body = message['body']
            mid = body['mid']
            if not isinstance(mid, str) or not 1 <= len(mid) <= 256:
                raise ValueError
            identity = kind + mid
            text = body.get('text') or ''
            photos = body.get('attachments') or []
            if not isinstance(text, str) or len(text) > 4000 or not isinstance(photos, list) or len(photos) > 20:
                raise ValueError
            # Do not persist tokens from arbitrary attachments, forwards or user profiles.
            photos = [{'type': a.get('type'), 'url': a.get('payload', {}).get('url')} for a in photos]
            payload = {'kind': kind, 'text': text, 'photos': photos}
        else:
            callback = data['callback']
            timestamp = callback.get('timestamp')
            sender = callback['user']['user_id']
            callback_id = callback['callback_id']
            if not isinstance(callback_id, str) or not 1 <= len(callback_id) <= 256:
                raise ValueError
            identity = kind + callback_id
            text = callback.get('payload') or ''
            if not isinstance(text, str) or len(text) > 256:
                raise ValueError
            payload = {'kind': kind, 'text': text, 'photos': [], 'callback_id': callback_id}
    else:
        return None
    if type(sender) is not int or not 0 < sender < 2**63 or type(chat) is not int or not -2**63 < chat < 2**63 or chat == 0:
        raise ValueError
    if type(timestamp) is not int or not 0 <= timestamp < 2**63:
        raise ValueError
    payload['timestamp'] = timestamp
    if len(identity) > 1000:
        raise ValueError
    return links.digest(json.dumps([sender, chat, identity])), sender, chat, payload


@csrf_exempt
@require_POST
def webhook(request):
    secret = settings.ARMADA_MAX_WEBHOOK_SECRET
    if not enabled() or len(secret) < 32:
        return HttpResponse(status=503)
    supplied = request.headers.get('X-Max-Bot-Api-Secret', '')
    if not hmac.compare_digest(supplied.encode(), secret.encode()):
        return HttpResponse(status=403)
    if request.content_type != 'application/json':
        return HttpResponse(status=415)
    try:
        if int(request.META.get('CONTENT_LENGTH') or 0) > 65536:
            return HttpResponse(status=413)
        raw = request.body
        if len(raw) > 65536:
            return HttpResponse(status=413)
        event = normalize(json.loads(raw))
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        return HttpResponse(status=400)
    if event is None:
        return HttpResponse(status=200)
    key, sender, chat, payload = event
    with transaction.atomic():
        if Inbox.objects.filter(key=key).exists():
            return HttpResponse(status=200)
        minute = int(timezone.now().timestamp()) // 60
        # Global row serialises intake across web workers, bounding total and sender rates.
        global_bucket, _ = RateBucket.objects.get_or_create(key=f'g:{minute}')
        global_bucket = RateBucket.objects.select_for_update().get(pk=global_bucket.pk)
        bucket, _ = RateBucket.objects.get_or_create(key=f'{sender}:{minute}')
        if global_bucket.count >= 600 or bucket.count >= 30:
            return HttpResponse(status=429)
        Inbox.objects.get_or_create(key=key, defaults={'max_user': sender, 'chat': chat, 'payload': payload})
        global_bucket.count += 1
        global_bucket.save(update_fields=['count'])
        bucket.count += 1
        bucket.save(update_fields=['count'])
    return HttpResponse(status=200)
