"""Queue only in the web process. Host-side worker owns consistent database/files capture."""
import uuid
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError,PermissionDenied
from django.views.decorators.debug import sensitive_variables
from accounts.models import User
from accounts.views import audit
from postoffice.crypto import seal,reveal
from .models import DiskBackupSettings,DiskBackupJob
from . import disk_crypto
from .yandex_disk import Client,DiskError,ROOT

def admin(user):
    fresh=User.objects.select_for_update(no_key=True).get(pk=user.pk)
    if not fresh.is_active or not fresh.is_leader or fresh.session_version!=user.session_version:raise PermissionDenied
    return fresh

def configured(config):
    if not config.credential or config.checked_version!=config.version:raise ValidationError('Сохраните токен и проверьте доступ к Диску.')
    if not config.public_key or not config.key_confirmed:raise ValidationError('Создайте и сохраните ключ восстановления, затем подтвердите сохранение.')

@transaction.atomic
@sensitive_variables('token')
def save(user,expected,token,enabled,backup_time):
    actor=admin(user);config,_=DiskBackupSettings.objects.select_for_update().get_or_create(pk=1)
    if config.version!=expected:raise ValidationError('Настройки изменены. Откройте страницу заново.')
    token=token.strip()
    if token:
        Client(token)  # validate header shape locally, no live request
        config.credential=seal(token);config.checked_version=0;config.checked_at=None
    if enabled:
        configured(config)
        if not config.worker_seen_at or config.worker_seen_at<timezone.now()-timedelta(minutes=15):raise ValidationError('Сначала подключите обработчик резервирования на VPS и дождитесь его проверки.')
    checked=config.checked_version==config.version
    config.version+=1
    if checked:config.checked_version=config.version
    config.enabled=enabled;config.backup_time=backup_time;config.not_before=timezone.now();config.updated_by=actor;config.error='';config.save()
    audit(actor,'disk_backup_settings',enabled=config.enabled,token_changed=bool(token))
    return config

@transaction.atomic
@sensitive_variables('private')
def create_key(user,expected):
    actor=admin(user);config,_=DiskBackupSettings.objects.select_for_update().get_or_create(pk=1)
    if config.version!=expected:raise ValidationError('Настройки изменены.')
    if config.key_confirmed or DiskBackupJob.objects.exists():raise ValidationError('Ключ уже используется. Сохраните существующий файл восстановления; замена требует отдельного переноса.')
    private,public,fingerprint=disk_crypto.generate()
    config.public_key=public;config.key_fingerprint=fingerprint;config.key_confirmed=False;config.enabled=False;config.updated_by=actor;config.save()
    audit(actor,'disk_backup_key_created',fingerprint=fingerprint)
    return private,fingerprint

@transaction.atomic
def confirm_key(user,fingerprint):
    actor=admin(user);config=DiskBackupSettings.objects.select_for_update().get(pk=1)
    if not config.public_key or config.key_fingerprint!=fingerprint:raise ValidationError('Ключ изменён. Сохраните актуальный файл.')
    config.key_confirmed=True;config.updated_by=actor;config.save()
    audit(actor,'disk_backup_key_confirmed',fingerprint=fingerprint)

@sensitive_variables('token')
def check(user,expected):
    with transaction.atomic():
        admin(user);config=DiskBackupSettings.objects.get(pk=1)
        if config.version!=expected:raise ValidationError('Настройки изменены.')
        credential=config.credential
    if not credential:raise ValidationError('Сначала сохраните токен.')
    try:
        token=reveal(credential);total,used=Client(token).info()
    except (DiskError,ValidationError):raise ValidationError('Не удалось проверить Диск. Проверьте токен, разрешения на папку приложения и информацию о Диске.') from None
    with transaction.atomic():
        actor=admin(user);config=DiskBackupSettings.objects.select_for_update().get(pk=1)
        if config.version!=expected:raise ValidationError('Токен или настройки изменились во время проверки.')
        config.checked_version=config.version;config.checked_at=timezone.now();config.total_space=total;config.used_space=used;config.error='';config.save()
        audit(actor,'disk_backup_connection_checked')
    return config

@transaction.atomic
def enqueue(user,token,day=None):
    actor=admin(user);config,_=DiskBackupSettings.objects.select_for_update().get_or_create(pk=1);configured(config)
    previous=DiskBackupJob.objects.filter(pk=token).first()
    if previous:
        if previous.author_id!=actor.pk:raise PermissionDenied
        return previous
    if day and DiskBackupJob.objects.filter(automatic_day=day).exists():return None
    if DiskBackupJob.objects.filter(state__in=['queued','running']).exists():raise ValidationError('Резервная копия уже ожидает обработки или создаётся.')
    return DiskBackupJob.objects.create(pk=token,config_version=config.version,public_key=config.public_key,
        key_fingerprint=config.key_fingerprint,author=actor,automatic_day=day)

@transaction.atomic
def claim(now=None):
    now=now or timezone.now()
    config,_=DiskBackupSettings.objects.select_for_update().get_or_create(pk=1)
    config.worker_seen_at=now;config.save(update_fields=['worker_seen_at'])
    DiskBackupJob.objects.filter(state='running',started_at__lt=now-timedelta(hours=24)).update(state='failed',error='Обработчик прерван. Проверьте локальные и удалённые копии.')
    if DiskBackupJob.objects.filter(state='running').exists():return None
    if config.enabled and config.updated_by_id:
        local=now.astimezone(ZoneInfo('Asia/Omsk'))
        due=datetime.combine(local.date(),config.backup_time,tzinfo=local.tzinfo)
        if due<=local and due>=config.not_before and not DiskBackupJob.objects.filter(automatic_day=local.date()).exists() and not DiskBackupJob.objects.filter(state='queued').exists():
            try:
                configured(config)
                if not config.updated_by.is_active or not config.updated_by.is_leader:raise PermissionDenied
                DiskBackupJob.objects.create(config_version=config.version,public_key=config.public_key,key_fingerprint=config.key_fingerprint,author=config.updated_by,automatic_day=local.date())
            except (ValidationError,PermissionDenied):
                config.enabled=False;config.error='Расписание остановлено: проверьте доступ, токен и ключ восстановления.';config.save()
    for job in DiskBackupJob.objects.select_for_update().filter(state='queued').order_by('created_at'):
        allowed=job.author and job.author.is_active and job.author.is_leader
        if job.config_version!=config.version or job.automatic_day and not config.enabled or not allowed:
            job.state='failed';job.error='Настройки или доступ изменились. Создайте новое задание.';job.save();continue
        configured(config)
        job.state='running';job.started_at=now;job.save()
        return job.pk
    return None

def fail(pk):
    DiskBackupJob.objects.filter(pk=pk,state='running').update(state='failed',error='Резервирование не завершено. Локальные копии не удалены; проверьте обработчик и подключение Диска.')

@sensitive_variables('token')
def upload(pk,source,work):
    job=DiskBackupJob.objects.get(pk=pk,state='running')
    config=DiskBackupSettings.objects.get(pk=1)
    if job.config_version!=config.version:raise ValidationError('Настройки изменились после начала резервирования.')
    configured(config)
    source=Path(source);work=Path(work)
    if not source.is_file() or source.stat().st_size==0 or not work.is_dir():raise ValidationError('Не найдена завершённая локальная копия или рабочий каталог.')
    target=work/(str(job.pk)+'.encrypted')
    created=False
    try:
        sha,size=disk_crypto.encrypt(source,target,job.public_key)
        created=True
        token=reveal(config.credential);client=Client(token)
        total,used=client.info()
        if total-used<size:raise DiskError('Недостаточно места на Диске.')
        path=ROOT+'/backups/'+str(job.created_at.date())+'-'+str(job.pk)+'.encrypted'
        client.mkdirs()
        try:client.metadata(path)
        except FileNotFoundError:client.upload(path,target)
        client.verify(path,sha,size)
        # The encrypted object was downloaded in full and hash-checked. Private key stays offline.
        DiskBackupJob.objects.filter(pk=pk,state='running').update(state='verified',remote_path=path,sha256=sha,size=size,verified_at=timezone.now(),error='')
        DiskBackupSettings.objects.filter(pk=1,version=config.version).update(total_space=total,used_space=used,error='')
    except BaseException:
        fail(pk);raise
    finally:
        if created:target.unlink(missing_ok=True)
