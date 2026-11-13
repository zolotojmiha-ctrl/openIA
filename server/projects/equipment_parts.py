"""Per-machine maintenance catalogue. Search is explicit navigation, not a purchase."""
from urllib.parse import urlencode
from django.core.exceptions import ValidationError
from django.db import transaction
from accounts.views import audit
from .models import EquipmentMaintenancePart
from .equipment import manager,visible_equipment

FIELDS=('name','article','manufacturer','quantity','unit','note','archived')


def search_links(part):
    query=' '.join(s for s in (part.manufacturer.strip(),part.article.strip()) if s)
    return {'yandex':'https://yandex.ru/search/?'+urlencode({'text':query}),
            'ozon':'https://www.ozon.ru/search/?'+urlencode({'text':query}),
            'wildberries':'https://www.wildberries.ru/catalog/0/search.aspx?'+urlencode({'search':query})}


def snapshot(part):
    return {key:format(getattr(part,key).normalize(),'f') if key=='quantity' and getattr(part,key) is not None else getattr(part,key) for key in FIELDS}


@transaction.atomic
def save(user,equipment_id,pk,expected,data,token):
    actor=manager(user)
    equipment=visible_equipment(actor).select_for_update(of=('self',),no_key=True).get(pk=equipment_id)
    if equipment.status=='archived':raise ValidationError('Техника находится в архиве. Сначала восстановите карточку.')
    row=EquipmentMaintenancePart.objects.select_for_update().get(pk=pk,equipment=equipment) if pk else None
    values={key:data.get(key) for key in FIELDS}
    for key in ('name','article','manufacturer','unit','note'):values[key]=str(values.get(key) or '').strip()
    values['archived']=bool(values['archived'])
    for key in ('name','article','manufacturer','unit'):
        if any(ord(c)<32 or ord(c)==127 for c in values[key]):raise ValidationError('Артикул и название должны быть записаны без переносов и служебных символов.')
    if not values['name'] or not values['article']:raise ValidationError('Укажите название запчасти и артикул.')
    if not values['unit']:values['unit']='шт.'
    if row and row.version!=expected:raise ValidationError('Запчасть уже изменили. Откройте актуальную запись.')
    if not row:
        previous=EquipmentMaintenancePart.objects.filter(creation_token=token).first()
        if previous:
            comparable={**values,'quantity':format(values['quantity'].normalize(),'f') if values['quantity'] is not None else None}
            if previous.equipment_id!=equipment.pk or previous.created_by_id!=actor.pk or snapshot(previous)!=comparable:raise ValidationError('Эта отправка уже использована для другой записи.')
            return previous
    before=snapshot(row) if row else None
    if not row:row=EquipmentMaintenancePart(equipment=equipment,creation_token=token,created_by=actor,updated_by=actor)
    else:row.version+=1
    for key,value in values.items():setattr(row,key,value)
    row.updated_by=actor;row.full_clean();row.save()
    audit(actor,'equipment_updated',kind='maintenance_part',equipment=str(equipment.pk),part=str(row.pk),part_version=row.version,previous=before,values=snapshot(row))
    return row
