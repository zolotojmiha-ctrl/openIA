from pathlib import Path
import unicodedata
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.db.models import Max
from accounts.models import User
from accounts.views import audit
from projects.models import LegalEntity,COMPANY_TAX_ID
from .models import Document,DocumentVersion,DocumentAccess,Bundle,DocumentRead,UploadReceipt
from .access import visible_documents,can_modify,unsorted_documents
from .storage import store_file,private_path

class Conflict(Exception):pass


def make_version(doc,user,data,comment='',key=None):
    v=DocumentVersion.objects.create(document=doc,sequence=(doc.versions.aggregate(n=Max('sequence'))['n'] or 0)+1,
        storage_key=key or store_file(data),original_name=data['name'],name_key=unicodedata.normalize('NFC',data['name']).casefold(),content_type=data['content_type'],size=data['size'],sha256=data['sha256'],uploaded_by=user,comment=comment,status='available')
    doc.current_version=v;doc.revision+=1;doc.save(update_fields=['current_version','revision'])
    audit(user,'document_replaced' if v.sequence>1 else 'document_uploaded',entity_id=str(doc.pk),version_id=str(v.pk),sha256=v.sha256)
    return v


def matches(user,data):
    qs=visible_documents(user).filter(archived=False,bundle=data.get('bundle'),current_version__name_key=unicodedata.normalize('NFC',data['file_name']).casefold())
    if not data.get('bundle'):
        qs=qs.filter(section=data['section'])
        for key,field in [('linked_objects','objects_linked'),('linked_contracts','contracts_linked'),('linked_people','personnel_linked')]:
            ids=[obj.pk for obj in data.get(key,[])]
            if ids:qs=qs.filter(**{field+'__in':ids})
            else:qs=qs.filter(**{field+'__isnull':True})
    return qs.distinct().order_by('id')


def upload_one(user,form):
    key=None
    try:
        with transaction.atomic():
            user=User.objects.select_for_update(no_key=True).get(pk=user.pk)
            from .access import has_permission
            if not has_permission(user,'document.upload'):raise PermissionDenied
            d=form.cleaned_data
            if d.get('upload_token'):
                prior=UploadReceipt.objects.filter(user=user,token=d['upload_token']).select_related('document').first()
                if prior:return prior.document
            if d.get('bundle_title') and not d.get('bundle'):
                d['bundle']=Bundle.objects.create(title=d['bundle_title'],created_by=user,legal_entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID))
            if d.get('bundle'):
                Bundle.objects.select_for_update().get(pk=d['bundle'].pk)
                if not (user.is_leader or d['bundle'].created_by_id==user.pk):raise PermissionDenied
            data=dict(form.file_data)
            found=list(matches(user,{**d,'file_name':data['name']}))
            if found and d['collision']=='ask':raise Conflict('Файл с таким названием уже есть. Выберите «Заменить» или «Оставить оба».')
            if found and d['collision']=='replace':
                candidates=[doc for doc in found if not d.get('collision_id') or doc.pk==d['collision_id']]
                if len(candidates)!=1:raise Conflict('Найдено несколько документов. Откройте нужный документ и загрузите новую версию из его карточки.')
                doc=Document.objects.select_for_update().get(pk=candidates[0].pk)
                if not can_modify(user,doc,'document.replace'):raise PermissionDenied
            else:
                if found:
                    original=data['name'];suffix=Path(original).suffix;stem=original[:-len(suffix)] if suffix else original;n=2
                    while matches(user,{**d,'file_name':data['name']}).exists():
                        data['name']=f'{stem[:170]} ({n}){suffix[:20]}';n+=1
                if d.get('is_primary') and d.get('bundle'):
                    Document.objects.filter(bundle=d['bundle'],is_primary=True).update(is_primary=False)
                doc=Document.objects.create(legal_entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID),created_by=user,title=d.get('title') or data['name'],section=d['section'],number=d.get('number',''),date=d.get('date'),kind='work',bundle=d.get('bundle'),is_primary=d.get('is_primary',False),link_not_required=d.get('link_not_required',False))
                doc.objects_linked.set(d['linked_objects']);doc.contracts_linked.set(d['linked_contracts']);doc.personnel_linked.set(d['linked_people'])
                if unsorted_documents(user).filter(pk=doc.pk).exists() and not d.get('defer_link'):
                    raise Conflict('Не выбрана ожидаемая связь. Укажите её или отметьте «Сохранить без связи во „На разбор“».')
                for recipient in d.get('recipients',[]):
                    DocumentAccess.objects.create(document=doc,user=recipient,enabled=True,can_download=True,can_history=True,granted_by=user)
            key=store_file(data);make_version(doc,user,data,d.get('comment',''),key=key)
            if d.get('upload_token'):UploadReceipt.objects.create(user=user,token=d['upload_token'],document=doc)
            return doc
    except Exception:
        if key:private_path(key).unlink(missing_ok=True)
        raise
