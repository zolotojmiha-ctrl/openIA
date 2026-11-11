"""MAX transport: fixed API origin, bounded bodies, no secret in file requests."""
import io
import ipaddress
import json
import socket
import ssl
import warnings
from pathlib import Path
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image, UnidentifiedImageError

API = 'https://platform-api2.max.ru'
API_CA_FILE = Path('/run/armada/max-api-ca.pem')


class DeliveryError(Exception):
    def __init__(self, state='unknown', retry_after=None):
        self.state = state
        self.retry_after = retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_ssl_context():
    """Additional operator-provided CA applies only to the fixed MAX API."""
    context = ssl.create_default_context()
    if API_CA_FILE.exists():
        context.load_verify_locations(cafile=str(API_CA_FILE))
    return context


def api(method, path, body=None):
    if not settings.ARMADA_MAX_TOKEN:
        raise DeliveryError('failed')
    request = Request(API + path, data=json.dumps(body).encode() if body is not None else None,
                      headers={'Authorization': settings.ARMADA_MAX_TOKEN, 'Content-Type': 'application/json'}, method=method)
    try:
        with build_opener(NoRedirect(), HTTPSHandler(context=api_ssl_context())).open(request, timeout=15) as response:
            result = response.read(1024 * 1024 + 1)
            if len(result) > 1024 * 1024:
                raise DeliveryError()
            return json.loads(result)
    except HTTPError as exc:
        # A timeout/5xx may mean MAX accepted the message: never retry blindly.
        retry_after = None
        if exc.code == 429:
            value = exc.headers.get('Retry-After', '') if exc.headers else ''
            try:
                retry_after = max(0, min(86400, int(value)))
            except (ValueError, TypeError):
                try:
                    date = parsedate_to_datetime(value)
                    retry_after = max(0, min(86400, int((date - datetime.now(timezone.utc)).total_seconds())))
                except (ValueError, TypeError, OverflowError):
                    pass
        raise DeliveryError('pending' if exc.code == 429 else 'failed' if 400 <= exc.code < 500 else 'unknown', retry_after) from None
    except (OSError, URLError, ValueError):
        raise DeliveryError() from None


def send(chat, body):
    if body.get('_callback_id'):
        result = api('POST', '/answers?' + urlencode({'callback_id': body['_callback_id']}),
                     {'notification': body['notification']})
        if not isinstance(result, dict) or result.get('success') is not True:
            raise DeliveryError()
        return
    result = api('POST', '/messages?' + urlencode({'chat_id': chat}), body)
    if not isinstance(result, dict) or not result.get('message', {}).get('body', {}).get('mid'):
        raise DeliveryError()


def download_photo(url):
    """CDN hosts must be explicitly verified by the operator during the pilot."""
    if not isinstance(url, str) or len(url) > 4000:
        raise ValidationError('Недопустимый адрес фотографии.')
    parsed = urlsplit(url)
    try:
        valid = (parsed.scheme == 'https' and parsed.hostname in settings.ARMADA_MAX_PHOTO_HOSTS
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password and not parsed.fragment)
        if not valid:
            raise ValueError
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError
    except (ValueError, OSError):
        raise ValidationError('Источник фото ещё не разрешён. Загрузите фото в карточке ЛК.') from None
    try:
        # Separate opener/request: the bot token is NEVER supplied to CDN requests.
        with build_opener(NoRedirect()).open(Request(url), timeout=10) as response:
            content = response.read(10 * 1024 * 1024 + 1)
        if not content or len(content) > 10 * 1024 * 1024:
            raise ValueError
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if image.format not in ('PNG', 'JPEG'):
                    raise ValueError
                if image.width * image.height > 16_000_000 or max(image.size) > 8000:
                    raise ValueError
                suffix = 'png' if image.format == 'PNG' else 'jpg'
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image.load()
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ValidationError('Фото не удалось проверить. Отправьте исправный PNG/JPEG до 10 МБ или загрузите через ЛК.') from None
    return SimpleUploadedFile('photo.' + suffix, content, content_type='image/' + ('png' if suffix == 'png' else 'jpeg'))
