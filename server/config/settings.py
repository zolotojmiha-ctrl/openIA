import os
import secrets
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit, unquote
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parents[2]
DEVELOPMENT = os.environ.get('ARMADA_DEVELOPMENT') == '1'
LAN_TEST = os.environ.get('ARMADA_LAN_TEST') == '1'

def read_secret(name):
    if os.environ.get(name):
        return os.environ[name]
    path=os.environ.get(name+'_FILE')
    return Path(path).read_text().strip() if path else ''
DEBUG = DEVELOPMENT
RUNTIME = Path(os.environ.get('ARMADA_RUNTIME_DIR', BASE_DIR / '.runtime'))
if DEVELOPMENT:
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    key_file = RUNTIME / 'secret-key'
    if not key_file.exists():
        try:
            with open(key_file, 'x', opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
                f.write(secrets.token_urlsafe(64))
        except FileExistsError:
            pass
    SECRET_KEY = key_file.read_text().strip()
else:
    SECRET_KEY = read_secret('ARMADA_SECRET_KEY')
    if len(SECRET_KEY) < 50:
        raise ImproperlyConfigured('Set ARMADA_SECRET_KEY (at least 50 random characters) securely.')
ALLOWED_HOSTS = [v.strip() for v in os.environ.get('ARMADA_ALLOWED_HOSTS', 'localhost,127.0.0.1' if DEVELOPMENT else '').split(',') if v.strip()]
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured('Set ARMADA_ALLOWED_HOSTS.')
if LAN_TEST:
    private_networks=[ipaddress.ip_network(n) for n in ['127.0.0.0/8','10.0.0.0/8','172.16.0.0/12','192.168.0.0/16']]
    for host in ALLOWED_HOSTS:
        try:
            address=ipaddress.ip_address(host)
        except ValueError:
            raise ImproperlyConfigured('LAN test hosts must be explicit local IPv4 addresses.')
        if address.version != 4 or not any(address in network for network in private_networks):
            raise ImproperlyConfigured('LAN test is restricted to local IPv4 addresses.')
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles', 'accounts', 'projects', 'documents', 'finance', 'repairs', 'postoffice']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'whitenoise.middleware.WhiteNoiseMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.common.CommonMiddleware', 'repairs.middleware.FieldUploads', 'django.middleware.csrf.CsrfViewMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware', 'accounts.middleware.AccountStateMiddleware', 'django.contrib.messages.middleware.MessageMiddleware', 'django.middleware.clickjacking.XFrameOptionsMiddleware']
ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'
TEMPLATES = [{'BACKEND':'django.template.backends.django.DjangoTemplates', 'DIRS':[BASE_DIR/'server/templates'], 'APP_DIRS':True, 'OPTIONS':{'context_processors':['django.template.context_processors.request','django.contrib.auth.context_processors.auth','django.contrib.messages.context_processors.messages','accounts.context.local_testing']}}]
url = read_secret('DATABASE_URL')
if url:
    u = urlsplit(url)
    if u.scheme not in ('postgres', 'postgresql'):
        raise ImproperlyConfigured('DATABASE_URL must use PostgreSQL.')
    DATABASES = {'default':{'ENGINE':'django.db.backends.postgresql','NAME':unquote(u.path.lstrip('/')),'USER':unquote(u.username or ''),'PASSWORD':unquote(u.password or ''),'HOST':u.hostname,'PORT':u.port or 5432,'CONN_MAX_AGE':60}}
elif DEVELOPMENT:
    DATABASES = {'default':{'ENGINE':'django.db.backends.sqlite3', 'NAME':RUNTIME/'development.sqlite3', 'OPTIONS':{'timeout':20}}}
else:
    raise ImproperlyConfigured('Set DATABASE_URL for the production PostgreSQL database.')
AUTH_USER_MODEL = 'accounts.User'
AUTH_PASSWORD_VALIDATORS = [{'NAME':'django.contrib.auth.password_validation.MinimumLengthValidator','OPTIONS':{'min_length':6}}]
PASSWORD_HASHERS = ['django.contrib.auth.hashers.Argon2PasswordHasher', 'django.contrib.auth.hashers.PBKDF2PasswordHasher']
LANGUAGE_CODE = 'ru'
TIME_ZONE = 'Asia/Omsk'
WHITENOISE_USE_FINDERS = False
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = Path(os.environ.get('ARMADA_STATIC_ROOT', RUNTIME / 'static'))
STATICFILES_DIRS = [BASE_DIR/'server/static']
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = '/cabinet/login/'
LOGIN_REDIRECT_URL = '/cabinet/'
SESSION_COOKIE_NAME = 'armada_session'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = not (DEVELOPMENT or LAN_TEST)
CSRF_COOKIE_SECURE = not (DEVELOPMENT or LAN_TEST)
SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SECURE_SSL_REDIRECT = not (DEVELOPMENT or LAN_TEST)
SECURE_HSTS_SECONDS = 31536000 if not (DEVELOPMENT or LAN_TEST) else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'
DATA_UPLOAD_MAX_MEMORY_SIZE = 128 * 1024
# Only enable a forwarded scheme header when a trusted proxy overwrites it.
if os.environ.get('ARMADA_TRUST_PROXY') == '1':
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
CSRF_TRUSTED_ORIGINS = [v for v in os.environ.get('ARMADA_CSRF_ORIGINS','').split(',') if v]

PRIVATE_DOCUMENT_ROOT = Path(os.environ.get("PRIVATE_DOCUMENT_ROOT", BASE_DIR / ".runtime" / "documents"))

# Mail endpoints are operator-controlled; users cannot submit arbitrary hosts.
ARMADA_MAIL_DOMAIN=os.environ.get('ARMADA_MAIL_DOMAIN','armada-sibir.ru')
ARMADA_IMAP_HOST=os.environ.get('ARMADA_IMAP_HOST','imap.beget.com')
ARMADA_IMAP_PORT=int(os.environ.get('ARMADA_IMAP_PORT','993'))
ARMADA_SMTP_HOST=os.environ.get('ARMADA_SMTP_HOST','smtp.beget.com')
ARMADA_SMTP_STARTTLS=os.environ.get('ARMADA_SMTP_STARTTLS')=='1'
ARMADA_SMTP_PORT=int(os.environ.get('ARMADA_SMTP_PORT','587' if ARMADA_SMTP_STARTTLS else '465'))
ARMADA_MAIL_KEY=read_secret('ARMADA_MAIL_KEY')
if DEVELOPMENT and not ARMADA_MAIL_KEY:
    import base64
    mail_key=RUNTIME/'mail-key'
    try:
        with open(mail_key,'x',opener=lambda p,flags:os.open(p,flags,0o600)) as f:f.write(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
    except FileExistsError:pass
    ARMADA_MAIL_KEY=mail_key.read_text().strip()
