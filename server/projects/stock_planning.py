"""Physical receipts and explicit reservations, serialized with the stock ledger."""
import re, uuid
from collections import Counter
from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from accounts.views import audit
from repairs.models import Coupling, CouplingUse, METHODS
from repairs.access import visible_jobs
from .models import StockItem, StockBalance
from . import stock


def job_label(job):
    return f'{job.contract.number if job.contract else "Без контракта"} · {job.section.project_object.name} · {job.section.pipeline} · '+', '.join('км '+d.km+' № '+d.number for d in job.active_defects)


@transaction.atomic
def receive(user, data, warehouse, count, date, note, token):
    actor=stock.manager(user);stock.lock()
    # A receipt token is also the stable key for all pieces in a bulk delivery.
    from .models import StockMovement
    q=Decimal(count)
    if not q.is_finite() or q<=0:raise ValidationError('Количество должно быть положительным.')
    if data['kind']=='coupling' and (q!=q.to_integral_value() or q>500):raise ValidationError('За одно поступление — от 1 до 500 отдельных муфт.')
    existing=StockMovement.objects.filter(token=token).first()
    if existing:
        expected_count=int(count) if data['kind']=='coupling' else 1
        if (existing.created_by_id!=actor.pk or existing.destination_id!=warehouse.pk or existing.date!=date or existing.note!=note
            or existing.item.kind!=data['kind'] or existing.item.method!=(data.get('method') or '')
            or existing.item.length_mm!=data.get('length_mm') or existing.item.wall_mm!=data.get('wall_mm')
            or existing.item.diameter_mm!=data.get('diameter_mm') or existing.item.unit!=data['unit']
            or existing.item.name!=(data.get('name') or ('Муфта '+dict(METHODS).get(data.get('method'),'') if data['kind']=='coupling' else ''))
            or existing.item.receipt_origin!=(data.get('receipt_origin') or '').strip()
            or existing.item.serial!=(data.get('serial') or '') or existing.quantity!=(1 if data['kind']=='coupling' else count)
            or (expected_count>1 and not StockMovement.objects.filter(token=uuid.uuid5(token,str(expected_count-1))).exists())
            or StockMovement.objects.filter(token=uuid.uuid5(token,str(expected_count))).exists()):
            raise ValidationError('Этот запрос поступления уже сохранён с другими данными.')
        return existing.destination
    wh=stock.warehouses(actor).get(pk=warehouse.pk)
    if wh.archived:raise ValidationError('Склад в архиве.')
    if data['kind']=='coupling' and q>1 and data.get('serial'):raise ValidationError('Заводской номер укажите при поступлении одной муфты или заполните номера позже поштучно.')
    if wh.brigade_assignment_id:
        # This is a historical separate holder, not a restriction on receiving
        # a real delivery. Existing assignment identity and ledger are retained.
        from .crews import visible_assignments
        if not visible_assignments(actor).filter(pk=wh.brigade_assignment_id,start_date__lte=date).filter(Q(end_date__isnull=True)|Q(end_date__gte=date)).exists():raise ValidationError('Назначение бригады не действует на дату поступления.')
    n=int(q) if data['kind']=='coupling' else 1
    for index in range(n):
        values=dict(data);values['coupling']=None;values['sku']='ПР-'+str(uuid.uuid4())[:12];values['archived']=False
        values['name']=values.get('name') or ('Муфта '+dict(METHODS).get(values.get('method'),'') if data['kind']=='coupling' else '')
        catalogue=data.get('catalogue_item') if index==0 else None
        if catalogue:
            original=stock.items(actor).get(pk=catalogue.pk)
            if original.version!=data.get('catalogue_version') or original.movements.exists() or original.coupling_id:raise ValidationError('Карточка уже использована или изменена. Откройте поступление заново.')
            values['sku']=original.sku
            item=stock.save_item(actor,original.pk,original.version,values)
        else:item=stock.save_item(actor,None,None,values)
        item.receipt_warehouse=wh;item.receipt_date=date
        if item.kind=='coupling':item.individual=True
        item.save(update_fields=['individual','receipt_warehouse','receipt_date'])
        amount=Decimal(1) if item.individual else q
        stock.quantity(amount,item)
        stock.post_locked(actor,item,'receipt',amount,None,wh,date,note,token if index==0 else uuid.uuid5(token,str(index)))
    return wh


@transaction.atomic
def reserve(user, item_id, version, job_id):
    actor=stock.manager(user);stock.lock()
    item=stock.items(actor).get(pk=item_id)
    if item.version!=version:raise ValidationError('Карточка изменена. Обновите страницу.')
    if item.kind!='coupling' or item.archived:raise ValidationError('Выберите действующую муфту.')
    if item.coupling_id and CouplingUse.objects.filter(coupling_id=item.coupling_id).exists():raise ValidationError('Муфта уже смонтирована.')
    if job_id:
        job=visible_jobs(actor).select_for_update(of=('self',)).get(pk=job_id)
        if job.state in ('reported','confirmed') or job.contract and job.contract.status=='archived' or job.section.project_object.status=='archived':raise ValidationError('Работа уже выполнена или закрыта.')
        if item.reserved_job_id and item.reserved_job_id!=job.pk:raise ValidationError('Муфта уже закреплена. Сначала снимите назначение.')
        if item.method!=job.method or Decimal(item.wall_mm or 0)!=job.wall_mm:raise ValidationError('Тип или толщина стенки муфты не соответствуют работе.')
        if job.section.diameter_mm and item.diameter_mm!=job.section.diameter_mm:raise ValidationError('Диаметр муфты не соответствует нефтепроводу.')
        lengths=requirements(job)
        if lengths is None or item.length_mm not in lengths:raise ValidationError('Длина не соответствует составу конструкции. Уточните состав в карточке работы.')
        held_items=list(StockItem.objects.filter(reserved_job=job,deleted_at__isnull=True).exclude(pk=item.pk))
        held=[i.length_mm for i in held_items]
        held_couplings={i.coupling_id for i in held_items}
        held.extend(use.coupling.length_mm for use in job.installed_couplings.select_related('coupling') if use.coupling_id not in held_couplings)
        if Counter(held)[item.length_mm]>=Counter(lengths)[item.length_mm]:raise ValidationError('Для этой длины уже закреплено нужное количество муфт.')
        if item.deleted_at:raise ValidationError('Позиция удалена.')
        balances=list(item.balances.filter(quantity__gt=0).select_related('warehouse'))
        if not balances or not item.individual and (len(balances)!=1 or balances[0].quantity!=1):
            raise ValidationError('Это старая групповая позиция. Откройте её остаток и выделите отдельную муфту кнопкой «Выделить 1 муфту».')
        area=job.section.project_object
        if any(b.warehouse.brigade_assignment_id and b.warehouse.project_object_id!=area.pk for b in balances):raise ValidationError('Муфта у бригады другого участка. Сначала оформите перемещение.')
        if not item.coupling_id:
            coupling=Coupling(project_object=area,method=item.method,length_mm=item.length_mm,wall_mm=item.wall_mm,serial=item.serial,warehouse=balances[0].warehouse.name,source={'stock_created':True});coupling.full_clean();coupling.save();item.coupling=coupling
        elif item.coupling.project_object_id!=area.pk:
            item.coupling.project_object=area;item.coupling.full_clean();item.coupling.save(update_fields=['project_object'])
        item.individual=True;item.reserved_job=job
    else:item.reserved_job=None
    item.version+=1;item.save()
    audit(actor,'stock_reserved',item=str(item.pk),job=str(item.reserved_job_id or ''))
    return item


@transaction.atomic
def split(user,item_id,version,warehouse_id):
    actor=stock.manager(user);stock.lock();item=stock.items(actor).get(pk=item_id)
    if item.version!=version:raise ValidationError('Позиция изменена.')
    if item.kind!='coupling' or item.individual or item.coupling_id:raise ValidationError('Это уже отдельная муфта.')
    wh=stock.warehouses(actor).get(pk=warehouse_id)
    if not item.balances.filter(warehouse=wh,quantity__gte=1).exists():raise ValidationError('Муфты на этом складе нет.')
    values={k:getattr(item,k) for k in stock.ITEM_FIELDS};values.update(sku='ЕД-'+str(uuid.uuid4())[:12],coupling=None,archived=False)
    piece=stock.save_item(actor,None,None,values);piece.individual=True
    original=item.movements.filter(kind='receipt').order_by('created_at','pk').first()
    piece.receipt_origin=item.receipt_origin
    piece.receipt_warehouse=item.receipt_warehouse or (original.destination if original else None);piece.receipt_date=item.receipt_date or (original.date if original else None)
    piece.save(update_fields=['individual','receipt_warehouse','receipt_date','receipt_origin'])
    note='Выделение отдельной муфты из позиции '+str(item.pk)+' в '+str(piece.pk)
    stock.post_locked(actor,item,'loss',Decimal(1),wh,None,timezone.localdate(),note,uuid.uuid4(),automatic=True)
    stock.post_locked(actor,piece,'receipt',Decimal(1),None,wh,timezone.localdate(),note,uuid.uuid4(),automatic=True)
    item.version+=1;item.save(update_fields=['version'])
    audit(actor,'stock_piece_split',item=str(item.pk),piece=str(piece.pk),warehouse=str(wh.pk))
    return piece


@transaction.atomic
def delete(user,item_id,version,note):
    actor=stock.manager(user);stock.lock();item=stock.items(actor).get(pk=item_id)
    if item.version!=version:raise ValidationError('Позиция изменена. Обновите страницу.')
    if not note.strip():raise ValidationError('Укажите причину удаления.')
    if item.reserved_job_id:raise ValidationError('Сначала снимите закрепление за работой.')
    if item.coupling_id and CouplingUse.objects.filter(coupling_id=item.coupling_id).exists():raise ValidationError('Муфта смонтирована. Исправьте результат ремонта перед удалением.')
    balances=list(item.balances.filter(quantity__gt=0).select_related('warehouse'))
    for balance in balances:
        if not stock.warehouses(actor).filter(pk=balance.warehouse_id).exists():raise ValidationError('Склад недоступен.')
        stock.post_locked(actor,item,'loss',balance.quantity,balance.warehouse,None,timezone.localdate(),'Удаление ошибочной позиции: '+note,uuid.uuid4(),automatic=True)
    item.deleted_at=timezone.now();item.archived=True;item.version+=1;item.save(update_fields=['deleted_at','archived','version'])
    audit(actor,'stock_item_deleted',item=str(item.pk),note=note,balances_removed=[str(b.quantity) for b in balances])


def requirements(job):
    """Explicit composition in metres. Ambiguous text is never guessed."""
    if job.method not in ('p1','p2','p3','p4','p5u'):return []
    text=job.composition.strip().replace(',','.')
    try:
        if text:
            if not re.fullmatch(r'\d+(?:\.\d+)?(?:\s*\+\s*\d+(?:\.\d+)?)*',text):return None
            parts=[Decimal(p.strip()) for p in text.split('+')]
            if job.length_m is not None and sum(parts)!=job.length_m:return None
        elif job.length_m:parts=[job.length_m]
        else:return None
        if any(x<=0 or x*1000!=(x*1000).to_integral_value() for x in parts):return None
        return [int(x*1000) for x in parts]
    except (InvalidOperation,ValueError):return None
