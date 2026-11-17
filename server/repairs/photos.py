"""One gallery for work evidence; byte identity never implies visual equivalence."""
from .models import Attachment


def gallery(job,report_id=None):
    from django.db.models import Q
    groups={}
    rows=Attachment.objects.filter(report__job=job,content_type__in=('image/jpeg','image/png'),deleted_at__isnull=True).select_related('report').prefetch_related('reused_in_reports').order_by('report__created_at','pk')
    if report_id:rows=rows.filter(Q(report_id=report_id)|Q(reused_in_reports__pk=report_id)).distinct()
    for photo in rows:
        item=groups.setdefault((photo.sha256,photo.size),{'photo':photo,'reports':[]})
        for report in [photo.report,*photo.reused_in_reports.all()]:
            if all(saved.pk!=report.pk for saved in item['reports']):item['reports'].append(report)
    return list(groups.values())


def can_edit(user):
    from accounts.permissions import permitted
    return permitted(user,'repairs.photos.edit')


def can_delete(user):
    from accounts.permissions import permitted
    return permitted(user,'repairs.photos.delete')


def accessible(user):
    from .access import visible_jobs
    return Attachment.objects.filter(report__job__in=visible_jobs(user),deleted_at__isnull=True,content_type__in=('image/jpeg','image/png')).select_related('report__job__contract')


def manifest(photo):
    return {key:getattr(photo,key) for key in ('storage_key','name','caption','content_type','size','sha256','revision')}


def _authorize(user,photo,action):
    from django.core.exceptions import PermissionDenied,ValidationError
    if not (can_delete(user) if action=='delete' else can_edit(user)):raise PermissionDenied
    if not accessible(user).filter(pk=photo.pk).exists():raise PermissionDenied
    if photo.report.job.contract_id and photo.report.job.contract.status=='archived':raise ValidationError('Контракт находится в архиве. Сначала восстановите его.')


def _actor(user):
    from accounts.models import User
    from django.core.exceptions import PermissionDenied
    current=User.objects.select_for_update(no_key=True).get(pk=user.pk)
    if not current.is_active or current.session_version!=user.session_version:raise PermissionDenied
    return current


def preview(user,photo_id,action,*,name='',caption='',upload=None):
    from datetime import timedelta
    from django.db import transaction
    from django.core.exceptions import ValidationError
    from django.utils import timezone
    from documents.storage import validate_upload,store_photo_file as store_file,discard_file
    from .progress import check_images
    from .models import PhotoOperation,Job
    key=''
    try:
        with transaction.atomic():
            current=_actor(user)
            photo=accessible(current).get(pk=photo_id)
            Job.objects.select_for_update(no_key=True).get(pk=photo.report.job_id)
            photo=Attachment.objects.select_for_update(of=('self',)).select_related('report__job__contract').get(pk=photo.pk)
            if action not in ('edit','delete'):raise ValidationError('Неизвестное действие.')
            _authorize(current,photo,action)
            payload={}
            if action=='edit':
                import re
                name=re.sub(r'[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]','',str(name)).strip()
                if not name or len(name)>200 or len(caption)>1000:raise ValidationError('Название — от 1 до 200 символов, подпись — до 1000.')
                payload={'name':name,'caption':caption}
                if upload:
                    if upload.size>10*1024*1024:raise ValidationError('Фотография — до 10 МБ.')
                    data=validate_upload(upload);check_images([data]);key=store_file(data)
                    payload['file']={k:data[k] for k in ('content_type','size','sha256')}
            payload['before']=manifest(photo)
            payload['targets']=[{'id':str(row.pk),'revision':row.revision} for row in Attachment.objects.filter(report__job=photo.report.job,sha256=photo.sha256,size=photo.size,deleted_at__isnull=True,content_type__in=('image/jpeg','image/png')).order_by('pk')]
            payload['report_links']=sum(1+row.reused_in_reports.count() for row in Attachment.objects.filter(pk__in=[target['id'] for target in payload['targets']]))
            return PhotoOperation.objects.create(actor=current,photo=photo,action=action,expected_revision=photo.revision,payload=payload,storage_key=key,expires=timezone.now()+timedelta(minutes=30))
    except Exception:
        if key:discard_file(key)
        raise


def operation(user,pk):
    from django.core.exceptions import PermissionDenied
    from .models import PhotoOperation
    item=PhotoOperation.objects.select_related('photo__report__job__contract').get(pk=pk,actor_id=user.pk)
    if not user.is_active:raise PermissionDenied
    _authorize(user,item.photo,item.action)
    return item


def confirm(user,pk):
    from django.db import transaction
    from django.core.exceptions import ValidationError
    from django.utils import timezone
    from documents.storage import discard_file,photo_storage_lock
    from accounts.views import audit
    from .models import PhotoOperation,PhotoRevision,PhotoPurge,Job
    with transaction.atomic():
        current=_actor(user)
        initial=PhotoOperation.objects.get(pk=pk,actor=current)
        Job.objects.select_for_update(no_key=True).get(pk=initial.photo.report.job_id)
        item=PhotoOperation.objects.select_for_update(of=('self',)).select_related('photo__report__job__contract').get(pk=pk,actor=current)
        if item.state=='applied':
            # A repeated confirmation cannot perform a new mutation.
            from .access import visible_jobs
            if not visible_jobs(current).filter(pk=item.photo.report.job_id).exists():raise ValidationError('Работа недоступна.')
            if not (can_delete(current) if item.action=='delete' else can_edit(current)):raise ValidationError('Право отозвано.')
            return item
        _authorize(current,item.photo,item.action)
        if item.state!='preview' or item.expires<=timezone.now():raise ValidationError('Предпросмотр истёк или отменён. Выберите фотографию заново.')
        photo=Attachment.objects.select_for_update().get(pk=item.photo_id)
        if photo.revision!=item.expected_revision:raise ValidationError('Фотографию уже изменили. Откройте новый предпросмотр.')
        from projects.archive import writable
        writable([photo.report.job.contract_id])
        photo_storage_lock()
        targets=list(Attachment.objects.select_for_update().filter(report__job=photo.report.job,sha256=photo.sha256,size=photo.size,deleted_at__isnull=True,content_type__in=('image/jpeg','image/png')).order_by('pk'))
        if [{'id':str(row.pk),'revision':row.revision} for row in targets]!=item.payload['targets']:
            raise ValidationError('Состав связанных фотографий изменился. Откройте новый предпросмотр.')
        keys=[];purge_manifests={}
        for target in targets:
            PhotoRevision.objects.create(photo=target,actor=current,revision=target.revision,action=item.action,manifest=manifest(target),storage_key=target.storage_key,size=target.size,sha256=target.sha256)
            if item.action=='edit':
                target.name=item.payload['name'];target.caption=item.payload['caption']
                if item.storage_key:
                    target.storage_key=item.storage_key
                    for field,value in item.payload['file'].items():setattr(target,field,value)
            else:
                for version in target.versions.exclude(storage_key=''):
                    purge_manifests[version.storage_key]={'sha256':version.sha256,'size':version.size}
                keys.extend(target.versions.exclude(storage_key='').values_list('storage_key',flat=True));keys.append(target.storage_key)
                target.versions.update(storage_key='')
                target.storage_key='';target.deleted_at=timezone.now()
            target.revision+=1;target.save()
        item.state='applied';item.storage_key='';item.save(update_fields=['state','storage_key'])
        audit(current,'repair_photo_'+item.action,entity_id=str(photo.pk),job=str(photo.report.job_id),revision=photo.revision+1,operation=str(item.pk),attachments=[str(target.pk) for target in targets])
        for key,metadata in purge_manifests.items():
            PhotoPurge.objects.update_or_create(storage_key=key,defaults={**metadata,'state':'pending','available_at':timezone.now()})
        transaction.on_commit(lambda:[discard_file(key) for key in set(keys) if key])
        return item


def cancel(user,pk):
    from django.db import transaction
    from documents.storage import discard_file
    from .models import PhotoOperation
    with transaction.atomic():
        _actor(user)
        item=PhotoOperation.objects.select_for_update().get(pk=pk,actor_id=user.pk)
        if item.state!='preview':return
        key=item.storage_key;item.storage_key='';item.state='cancelled';item.save(update_fields=['state','storage_key'])
        if key:transaction.on_commit(lambda:discard_file(key))


def report_photo_count(report):
    from django.db.models import Q
    return Attachment.objects.filter(Q(report=report)|Q(reused_in_reports=report),deleted_at__isnull=True,content_type__in=('image/jpeg','image/png')).order_by().values('sha256','size').distinct().count()
