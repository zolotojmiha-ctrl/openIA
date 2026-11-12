"""Private readback-verified mirror. Local originals are retained by default."""
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.utils import timezone
from postoffice.crypto import reveal
from documents.storage import private_path
from .models import DiskBackupSettings,DiskBackupJob,PhotoArchive
from .yandex_disk import Client,ROOT,DiskError


def client():
    config=DiskBackupSettings.objects.get(pk=1)
    if not config.credential or config.checked_version!=config.version:raise DiskError('Проверьте подключение Диска в ЛК.')
    return Client(reveal(config.credential))


def archive_one(attachment):
    source=private_path(attachment.storage_key)
    from .disk_crypto import digest
    if digest(source)!=(attachment.sha256,attachment.size):raise OSError('Нарушена сохранность локального фото.')
    remote=ROOT+'/photos/'+attachment.storage_key.replace('/','-')+'-'+attachment.sha256
    disk=client();disk.mkdirs()
    try:disk.request('PUT','/resources',{'path':ROOT+'/photos'})
    except FileExistsError:pass
    try:disk.upload(remote,source)
    except FileExistsError:pass
    disk.verify(remote,attachment.sha256,attachment.size)
    # Keep the local source until explicit, separately requested removal after readback.
    row,_=PhotoArchive.objects.update_or_create(storage_key=attachment.storage_key,defaults={
        'sha256':attachment.sha256,'size':attachment.size,'remote_path':remote,'verified_at':timezone.now()})
    return row


def remote_file(version):
    row=PhotoArchive.objects.get(storage_key=version.storage_key,sha256=version.sha256,size=version.size)
    return client().download_verified(row.remote_path,row.sha256,row.size)


def step():
    config=DiskBackupSettings.objects.filter(pk=1,photos_enabled=True).first()
    if not config:return False
    from repairs.models import Attachment
    archived=PhotoArchive.objects.values('storage_key')
    item=Attachment.objects.filter(content_type__in=['image/jpeg','image/png']).exclude(storage_key__in=archived).order_by('pk').first()
    copied=False
    if item:archive_one(item);copied=True
    if config.photos_remove_local:prune_one()
    return copied


def prune_one():
    """Opt-in local cache removal after seven days AND a verified full backup."""
    from repairs.models import Attachment
    rows=PhotoArchive.objects.filter(local_removed_at__isnull=True).order_by('verified_at').iterator(chunk_size=100)
    for row in rows:
        attachment=Attachment.objects.filter(storage_key=row.storage_key,sha256=row.sha256,size=row.size,
            report__created_at__lte=timezone.now()-timedelta(days=7)).select_related('report').first()
        if not attachment:continue
        if not DiskBackupJob.objects.filter(state='verified',started_at__gte=attachment.report.created_at).exists():continue
        source=private_path(row.storage_key)
        if not source.exists():continue
        from .disk_crypto import digest
        if digest(source)!=(row.sha256,row.size):raise OSError('Локальное фото повреждено; не удалено.')
        client().verify(row.remote_path,row.sha256,row.size)
        source.unlink()
        row.local_removed_at=timezone.now();row.save(update_fields=['local_removed_at'])
        return True
    return False
