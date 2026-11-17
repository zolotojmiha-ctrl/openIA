"""Private immutable files; original names never form a filesystem path."""
import hashlib
import io
import os
from pathlib import Path
import re
import uuid
import unicodedata
import zipfile
from django.conf import settings
from django.core.exceptions import ValidationError

MAX_FILE_SIZE=32*1024*1024
TYPES={'.pdf':'application/pdf','.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document','.xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.txt':'text/plain'}

def validate_upload(upload):
    if not upload or upload.size<=0 or upload.size>MAX_FILE_SIZE:
        raise ValidationError('Выберите непустой файл размером до 32 МБ.')
    name=re.sub(r'[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]','',upload.name.replace('\\','/').split('/')[-1])[:200].strip()
    if not name:raise ValidationError('У файла должно быть название.')
    content=upload.read(MAX_FILE_SIZE+1);upload.seek(0)
    if not content or len(content)>MAX_FILE_SIZE:raise ValidationError('Файл превышает 32 МБ.')
    # Arbitrary files are opaque attachments. Never unpack, import or execute them.
    mime='application/octet-stream'
    suffix=Path(name).suffix.lower()
    if suffix=='.pdf' and content.startswith(b'%PDF-'):mime='application/pdf'
    elif suffix=='.png' and content.startswith(b'\x89PNG\r\n\x1a\n'):mime='image/png'
    elif suffix in ('.jpg','.jpeg') and content.startswith(b'\xff\xd8\xff'):mime='image/jpeg'
    elif suffix=='.txt' and b'\x00' not in content:
        try:content.decode('utf-8');mime='text/plain'
        except UnicodeDecodeError:pass
    return {'name':name,'content_type':mime,'size':len(content),'sha256':hashlib.sha256(content).hexdigest(),'content':content}

def private_path(key):
    if not re.fullmatch(r'[a-f0-9]{2}/[a-f0-9]{32}',key):raise ValueError('Недопустимый ключ файла.')
    root=Path(settings.PRIVATE_DOCUMENT_ROOT).resolve()
    path=(root/key).resolve()
    if not path.is_relative_to(root):raise ValueError('Недопустимый путь.')
    return path

def photo_storage_lock():
    """Serialize shared-photo reference creation and physical reclamation."""
    from django.db import connection
    if connection.vendor=='postgresql':
        with connection.cursor() as cursor:cursor.execute('SELECT pg_advisory_xact_lock(%s)',[6333001])


def storage_referenced(key,include_archive=True):
    from django.apps import apps
    for model in apps.get_models():
        label=model._meta.label_lower
        if label=='repairs.photopurge' or (label=='projects.photoarchive' and not include_archive):continue
        if not any(field.name=='storage_key' for field in model._meta.concrete_fields):continue
        rows=model._default_manager.filter(storage_key=key)
        if label=='documents.documentversion':rows=rows.filter(document__deleted_at__isnull=True)
        elif label=='documents.documentpart':rows=rows.filter(version__document__deleted_at__isnull=True)
        if rows.exists():return True
    return False


def discard_file(key):
    """Rollback/temporary-file cleanup must respect every live database reference."""
    from django.db import transaction
    with transaction.atomic():
        photo_storage_lock()
        if storage_referenced(key):return False
        private_path(key).unlink(missing_ok=True)
        return True


def store_photo_file(data):
    from django.db import connection
    # Reuse immutable bytes only within a transaction that also creates the
    # reference. Nontransactional callers retain their original ownership rules.
    if connection.in_atomic_block and data.get('content_type') in ('image/jpeg','image/png'):
        photo_storage_lock()
        from repairs.models import Attachment,PhotoRevision
        from maxbot.models import Photo
        from itertools import chain
        candidates=chain(*(model.objects.filter(sha256=data['sha256'],size=data['size']).exclude(storage_key='').order_by('pk') for model in (Attachment,PhotoRevision,Photo)))
        for candidate in candidates:
            try:
                with private_path(candidate.storage_key).open('rb') as existing:
                    if existing.read(MAX_FILE_SIZE+1)==data['content']:return candidate.storage_key
            except FileNotFoundError:continue
    return store_file(data)


def store_file(data):
    token=uuid.uuid4().hex
    key=token[:2]+'/'+token
    path=private_path(key)
    path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    with open(path,'xb',opener=lambda p,flags:os.open(p,flags,0o400)) as output:
        output.write(data['content']);output.flush();os.fsync(output.fileno())
    return key

def verified_file(version):
    try:file=private_path(version.storage_key).open('rb')
    except FileNotFoundError:
        from projects.photo_archive import remote_file
        try:return remote_file(version)
        except Exception:raise OSError('Фото временно недоступно в хранилище.') from None
    digest=hashlib.sha256()
    size=0
    for chunk in iter(lambda:file.read(65536),b''):
        digest.update(chunk);size+=len(chunk)
    if size!=version.size or digest.hexdigest()!=version.sha256:
        file.close();raise OSError('Нарушена сохранность файла.')
    file.seek(0)
    return file
