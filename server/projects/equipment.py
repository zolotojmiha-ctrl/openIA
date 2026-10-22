"""Equipment changes use explicit delegation; links never grant file access."""
from datetime import timedelta
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from accounts.models import User
from accounts.permissions import permitted
from accounts.views import audit
from documents.access import visible_documents
from .models import Equipment,EquipmentAssignment,EquipmentDocument,LegalEntity,COMPANY_TAX_ID
from .access import visible_objects,visible_contracts

def visible_equipment(user):
    qs=Equipment.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID)
    if not user.is_authenticated or not user.is_active:return qs.none()
    if user.role=='subcontractor' and not permitted(user,'equipment.manage'):return qs.none()
    if not permitted(user,'equipment.manage'):
        qs=qs.filter(placements__project_object__in=visible_objects(user))
    return qs if permitted(user,'equipment.manage') else qs.distinct()

def placements(user):
    qs=EquipmentAssignment.objects.filter(equipment__in=visible_equipment(user))
    if not user.is_leader:qs=qs.filter(project_object__in=visible_objects(user))
    return qs.select_related('equipment','project_object')

def current(qs,on=None):
    on=on or timezone.localdate()
    return qs.filter(start_date__lte=on).filter(Q(end_date__isnull=True)|Q(end_date__gte=on))

def equipment_files(user):
    return EquipmentDocument.objects.filter(equipment__in=visible_equipment(user),document__in=visible_documents(user)).select_related('document','equipment')

def due_documents(user):
    today=timezone.localdate()
    return [r for r in equipment_files(user).filter(active=True,remind=True,expires_on__isnull=False,equipment__status__in=['ready','working','repair','reserve'],document__archived=False).order_by('expires_on') if r.expires_on<=today+timedelta(days=r.days_before)]

def manager(user):
    actor=User.objects.select_for_update().get(pk=user.pk)
    if not permitted(actor,'equipment.manage') or actor.session_version!=user.session_version:raise PermissionDenied
    return actor

FIELDS=('name','category','inventory_number','registration_number','serial_number','ownership','owner','status','notes')
@transaction.atomic
def save_equipment(user,pk,version,data):
    actor=manager(user)
    company=LegalEntity.objects.select_for_update().get(tax_id=COMPANY_TAX_ID)
    item=visible_equipment(actor).select_for_update().get(pk=pk) if pk else Equipment(legal_entity=company,created_by=actor)
    if pk and item.version!=version:raise ValidationError('Карточка изменена. Обновите страницу.')
    before={k:getattr(item,k) for k in FIELDS} if pk else None
    for k in FIELDS:setattr(item,k,str(data.get(k,'')).strip())
    if item.status=='archived' and item.pk and item.placements.filter(Q(end_date__isnull=True)|Q(end_date__gte=timezone.localdate())).exists():raise ValidationError('Сначала завершите текущие и будущие назначения техники.')
    item.full_clean()
    if pk:item.version+=1
    item.save();audit(actor,'equipment_updated',equipment=str(item.pk),previous=before,values={k:getattr(item,k) for k in FIELDS},version=item.version)
    return item

def dates(start,end):
    if not start or end and end<start:raise ValidationError('Окончание не может быть раньше начала.')

@transaction.atomic
def assign(user,pk,version,obj,contract,start,end=None,note='',transfer=False):
    actor=manager(user);item=visible_equipment(actor).select_for_update().get(pk=pk)
    if item.version!=version or item.status=='archived':raise ValidationError('Техника изменена или находится в архиве.')
    dates(start,end)
    if obj.status=='archived' or obj.legal_entity_id!=item.legal_entity_id or not visible_objects(actor).filter(pk=obj.pk).exists():raise ValidationError('Объект недоступен или в архиве.')
    if contract and (contract.status=='archived' or not visible_contracts(actor).filter(pk=contract.pk,objects_linked=obj).exists()):raise ValidationError('Контракт недоступен или не связан с объектом.')
    if len(note)>1000:raise ValidationError('Пояснение — до 1000 символов.')
    overlaps=item.placements.filter(Q(end_date__isnull=True)|Q(end_date__gte=start))
    if end:overlaps=overlaps.filter(start_date__lte=end)
    previous=list(overlaps.select_for_update().order_by('pk'))
    if previous:
        if not transfer or len(previous)!=1 or previous[0].end_date or previous[0].start_date>=start:raise ValidationError('Период пересекается с другим назначением. Завершите прежний период или выберите перевод с другой даты.')
        old=previous[0]
        if not visible_objects(actor).filter(pk=old.project_object_id).exists():raise ValidationError('Нет доступа к прежнему объекту. Перевод должен выполнить уполномоченный сотрудник.')
        old.end_date=start-timedelta(days=1);old.version+=1;old.save(update_fields=['end_date','version'])
        audit(actor,'equipment_assigned',kind='transfer_from',assignment=str(old.pk),end=str(old.end_date))
    result=EquipmentAssignment.objects.create(equipment=item,project_object=obj,contract=contract,start_date=start,end_date=end,note=note,created_by=actor)
    item.version+=1;item.save(update_fields=['version'])
    audit(actor,'equipment_assigned',kind='assigned',equipment=str(pk),assignment=str(result.pk),object=str(obj.pk),contract=str(contract.pk) if contract else None,start=str(start),end=str(end or ''),note=note)
    return result

@transaction.atomic
def end_assignment(user,pk,version,end):
    actor=manager(user);original=placements(actor).get(pk=pk)
    item=visible_equipment(actor).select_for_update().get(pk=original.equipment_id)
    row=placements(actor).select_for_update(of=('self',)).get(pk=pk)
    if row.version!=version or row.end_date:raise ValidationError('Назначение уже изменено или завершено.')
    if not visible_objects(actor).filter(pk=row.project_object_id).exists():raise ValidationError('Объект недоступен.')
    if not end:raise ValidationError('Укажите последний день работы.')
    dates(row.start_date,end);row.end_date=end;row.version+=1;row.save(update_fields=['end_date','version'])
    item.version+=1;item.save(update_fields=['version'])
    audit(actor,'equipment_assigned',kind='ended',assignment=str(pk),equipment=str(item.pk),end=str(end))
    return row

@transaction.atomic
def save_file(user,equipment_id,pk,version,data):
    actor=manager(user);item=visible_equipment(actor).select_for_update().get(pk=equipment_id)
    row=equipment_files(actor).select_for_update(of=('self',)).get(pk=pk,equipment=item) if pk else EquipmentDocument(equipment=item,created_by=actor)
    if pk and row.version!=version:raise ValidationError('Документ уже изменён. Обновите страницу.')
    document=data['document']
    if not visible_documents(actor).filter(pk=document.pk,archived=False).exists():raise ValidationError('Документ недоступен или в архиве.')
    if pk and row.document_id!=document.pk:raise ValidationError('Для другого файла создайте новую связь.')
    before={'label':row.label,'expires_on':str(row.expires_on or ''),'remind':row.remind,'days_before':row.days_before,'active':row.active} if pk else None
    for key in ('document','label','expires_on','remind','days_before','active'):setattr(row,key,data[key])
    if row.remind and not row.expires_on:raise ValidationError('Для напоминания нужна дата окончания.')
    if not 0<=row.days_before<=365:raise ValidationError('Срок напоминания — от 0 до 365 дней.')
    row.full_clean()
    if pk:row.version+=1
    row.save();audit(actor,'equipment_document_updated',equipment=str(item.pk),link=str(row.pk),document=str(document.pk),previous=before,label=row.label,expires_on=str(row.expires_on or ''),remind=row.remind,days_before=row.days_before,active=row.active)
    return row
