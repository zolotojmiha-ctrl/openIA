"""Reference-aware reclamation after an explicit photo deletion."""
from datetime import timedelta
from django.apps import apps
from django.db import transaction
from django.utils import timezone
from documents.storage import private_path,photo_storage_lock,discard_file,storage_referenced
from .models import PhotoPurge,PhotoOperation


def expire_previews():
    for pk in PhotoOperation.objects.filter(state='preview',expires__lte=timezone.now()).values_list('pk',flat=True)[:30]:
        with transaction.atomic():
            item=PhotoOperation.objects.select_for_update().get(pk=pk)
            if item.state!='preview' or item.expires>timezone.now():continue
            key=item.storage_key;item.storage_key='';item.state='expired';item.save(update_fields=['storage_key','state'])
            if key:transaction.on_commit(lambda key=key:discard_file(key))


def step():
    expire_previews()
    with transaction.atomic():
        photo_storage_lock()
        item=PhotoPurge.objects.select_for_update(skip_locked=True).filter(state__in=('pending','error','shared'),available_at__lte=timezone.now()).order_by('created_at').first()
        if not item:return False
        if storage_referenced(item.pk,include_archive=False):
            item.state='shared';item.available_at=timezone.now()+timedelta(minutes=5);item.save(update_fields=['state','available_at']);return True
        from projects.models import PhotoArchive
        archive=PhotoArchive.objects.filter(storage_key=item.pk).first()
        if archive:
            from projects.photo_archive import client
            from projects.yandex_disk import ROOT,DiskError
            expected=ROOT+'/photos/'+item.pk.replace('/','-')+'-'+item.sha256
            if archive.sha256!=item.sha256 or archive.size!=item.size or archive.remote_path!=expected:
                item.state='error';item.available_at=timezone.now()+timedelta(minutes=5);item.save(update_fields=['state','available_at']);return False
            try:
                disk=client()
                try:disk.request('DELETE','/resources',{'path':expected,'permanently':'true'})
                except FileNotFoundError:pass
                try:disk.request('GET','/resources',{'path':expected,'fields':'path'})
                except FileNotFoundError:archive.delete()
                else:
                    item.available_at=timezone.now()+timedelta(minutes=1);item.save(update_fields=['available_at']);return False
            except Exception:
                # Never persist provider response bodies or credential-bearing URLs.
                item.state='error';item.available_at=timezone.now()+timedelta(minutes=5);item.save(update_fields=['state','available_at']);return False
        private_path(item.pk).unlink(missing_ok=True)
        item.state='done';item.save(update_fields=['state'])
        return True


def duplicate_plan():
    """Only locally verified exact bytes; names and visual resemblance are irrelevant."""
    import hashlib
    from collections import defaultdict
    from .models import Attachment,PhotoRevision
    from maxbot.models import Photo
    groups=defaultdict(set)
    sources=(Attachment.objects.filter(content_type__in=('image/jpeg','image/png')),PhotoRevision.objects.filter(manifest__content_type__in=('image/jpeg','image/png')),Photo.objects.filter(draft__action__in=('progress','repaired','unrepaired','result','correction','note','ddk','problem','resolve')))
    for rows in sources:
        for key,sha,size in rows.exclude(storage_key='').values_list('storage_key','sha256','size').iterator():groups[(sha,size)].add(key)
    pinned=set(Attachment.objects.filter(report__job__contract__status='archived').values_list('storage_key',flat=True))|set(PhotoRevision.objects.filter(photo__report__job__contract__status='archived').values_list('storage_key',flat=True))
    result=[]
    for (sha,size),keys in sorted(groups.items()):
        if len(keys)<2:continue
        valid=[]
        for key in sorted(keys):
            try:
                with private_path(key).open('rb') as source:
                    digest=hashlib.sha256();actual=0
                    for chunk in iter(lambda:source.read(65536),b''):digest.update(chunk);actual+=len(chunk)
                if actual==size and digest.hexdigest()==sha:valid.append(key)
            except FileNotFoundError:continue
        if len(valid)>1:
            keep=next((key for key in valid if key in pinned),valid[0])
            duplicates=[key for key in valid if key!=keep and key not in pinned]
            if duplicates:result.append({'sha256':sha,'size':size,'keep':keep,'duplicates':duplicates})
    return result


def plan_digest(plan):
    import hashlib,json
    return hashlib.sha256(json.dumps(plan,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def consolidate(expected):
    from django.core.exceptions import ValidationError
    from .models import Attachment,PhotoRevision
    from maxbot.models import Photo
    from projects.archive import writable
    keys=[]
    with transaction.atomic():
        # Match the application contract -> file-lock order. Archived evidence
        # is not rewritten by this ordinary maintenance operation.
        initial=duplicate_plan()
        duplicates=[key for group in initial for key in group['duplicates']]
        contract_ids=set(Attachment.objects.filter(storage_key__in=duplicates).values_list('report__job__contract_id',flat=True))|set(PhotoRevision.objects.filter(storage_key__in=duplicates).values_list('photo__report__job__contract_id',flat=True))
        writable(contract_ids)
        photo_storage_lock()
        plan=duplicate_plan()
        if plan_digest(plan)!=expected:raise ValidationError('Состав файлов изменился. Повторите предпросмотр.')
        for group in plan:
            for model in (Attachment,PhotoRevision,Photo):
                model.objects.filter(storage_key__in=group['duplicates'],sha256=group['sha256'],size=group['size']).update(storage_key=group['keep'])
            for key in group['duplicates']:
                PhotoPurge.objects.update_or_create(storage_key=key,defaults={'sha256':group['sha256'],'size':group['size'],'state':'pending','available_at':timezone.now()})
                keys.append(key)
        transaction.on_commit(lambda:[discard_file(key) for key in keys])
        return plan
