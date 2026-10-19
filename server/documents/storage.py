"""Private immutable files; original names never form a filesystem path."""
import hashlib
import io
import os
from pathlib import Path
import re
import uuid
import zipfile
from django.conf import settings
from django.core.exceptions import ValidationError

MAX_FILE_SIZE=10*1024*1024
TYPES={'.pdf':'application/pdf','.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document','.xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.txt':'text/plain'}

def validate_upload(upload):
    if not upload or upload.size<=0 or upload.size>MAX_FILE_SIZE:
        raise ValidationError('Выберите непустой файл размером до 10 МБ.')
    name=re.sub(r'[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]','',upload.name.replace('\\','/').split('/')[-1])[:200]
    suffix=Path(name).suffix.lower()
    if suffix not in TYPES:raise ValidationError('Допустимы PDF, DOCX, XLSX, PNG, JPG и TXT. Исполняемые файлы и архивы не принимаются.')
    content=upload.read(MAX_FILE_SIZE+1)
    upload.seek(0)
    if not content or len(content)>MAX_FILE_SIZE:raise ValidationError('Файл превышает 10 МБ.')
    valid=True
    if suffix=='.pdf':valid=content.startswith(b'%PDF-')
    elif suffix=='.png':valid=content.startswith(b'\x89PNG\r\n\x1a\n')
    elif suffix in ('.jpg','.jpeg'):valid=content.startswith(b'\xff\xd8\xff')
    elif suffix=='.txt':
        try:content.decode('utf-8')
        except UnicodeDecodeError:valid=False
        if b'\x00' in content:valid=False
    elif suffix in ('.docx','.xlsx'):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members=archive.infolist()
                names={entry.filename for entry in members}
                expected='word/document.xml' if suffix=='.docx' else 'xl/workbook.xml'
                valid=expected in names and '[Content_Types].xml' in names and len(members)<=2000 and sum(e.file_size for e in members)<=50*1024*1024
                for entry in members:
                    path=Path(entry.filename)
                    if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or entry.flag_bits&1 or entry.file_size>max(1,entry.compress_size)*200 or 'vbaproject' in entry.filename.lower() or '/embeddings/' in entry.filename.lower():valid=False
                if valid and archive.testzip() is not None:valid=False
        except (zipfile.BadZipFile,RuntimeError,NotImplementedError,EOFError):valid=False
    if not valid:raise ValidationError('Содержимое файла не соответствует формату либо содержит недопустимые вложения. Проверка не заменяет антивирус.')
    return {'name':name,'content_type':TYPES[suffix],'size':len(content),'sha256':hashlib.sha256(content).hexdigest(),'content':content}

def private_path(key):
    if not re.fullmatch(r'[a-f0-9]{2}/[a-f0-9]{32}',key):raise ValueError('Недопустимый ключ файла.')
    root=Path(settings.PRIVATE_DOCUMENT_ROOT).resolve()
    path=(root/key).resolve()
    if not path.is_relative_to(root):raise ValueError('Недопустимый путь.')
    return path

def store_file(data):
    token=uuid.uuid4().hex
    key=token[:2]+'/'+token
    path=private_path(key)
    path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    with open(path,'xb',opener=lambda p,flags:os.open(p,flags,0o400)) as output:
        output.write(data['content']);output.flush();os.fsync(output.fileno())
    return key

def verified_file(version):
    file=private_path(version.storage_key).open('rb')
    digest=hashlib.sha256()
    size=0
    for chunk in iter(lambda:file.read(65536),b''):
        digest.update(chunk);size+=len(chunk)
    if size!=version.size or digest.hexdigest()!=version.sha256:
        file.close();raise OSError('Нарушена сохранность файла.')
    file.seek(0)
    return file
