"""Resolve installation facts to real inventory under the shared stock lock.

Report authority permits this narrow posting, never general stock management.
"""
import uuid,base64
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from accounts.views import audit
from projects.models import StockItem, StockBalance
from projects import stock
from projects.repair_materials import eligible
from .models import Coupling, CouplingUse


def source_key(item_id,warehouse_id):
    return 'v1_'+base64.urlsafe_b64encode(uuid.UUID(str(item_id)).bytes+uuid.UUID(str(warehouse_id)).bytes).decode().rstrip('=')


def candidates(job, method, length, wall, serial='', existing=None):
    items = StockItem.objects.filter(legal_entity=job.section.project_object.legal_entity,
        kind='coupling', method=method, length_mm=length, wall_mm=wall,
        diameter_mm=job.section.diameter_mm, archived=False, deleted_at__isnull=True)
    items = items.filter(Q(coupling__isnull=True) | Q(coupling__installation__isnull=True) |
                         Q(coupling__installation__job=job))
    rows = StockBalance.objects.filter(item__in=items, quantity__gte=1).select_related(
        'item__coupling', 'item__reserved_job__section', 'warehouse__brigade_assignment').prefetch_related(
        'warehouse__served_objects','warehouse__served_brigades','item__reserved_job__defects')
    result = []
    for row in rows:
        item = row.item
        if not eligible(row.warehouse, job):
            continue
        number = item.serial or (item.coupling.serial if item.coupling_id else '')
        if number and number != serial:
            continue
        donor = item.reserved_job
        if donor and donor.pk != job.pk and (donor.state in ('reported','confirmed','unrepairable') or
                donor.contract_id != job.contract_id or donor.section.project_object_id != job.section.project_object_id):
            continue
        rank = 0 if number and number == serial else 1 if item.reserved_job_id == job.pk else 2 if not donor else 3
        result.append((rank, row))
    return [row for _, row in sorted(result, key=lambda pair: (
        pair[0], pair[1].item.sku, str(pair[1].warehouse_id)))]


def choose(job, method, length, wall, serial, source=None, used=()):
    rows = [r for r in candidates(job, method, length, wall, serial) if (r.quantity > used.get((r.item_id,r.warehouse_id),0) if isinstance(used,dict) else r.item_id not in used)]
    if source:
        rows = [r for r in rows if str(r.item_id) == str(source) or source_key(r.item_id,r.warehouse_id)==str(source)]
    if not rows:
        raise ValidationError('Нет совместимой складской муфты: проверьте тип, диаметр, длину, стенку и остаток. Монтаж без складского источника не сохранён.')
    first = rows[0]
    number = first.item.serial or (first.item.coupling.serial if first.item.coupling_id else '')
    if not source and not number and first.item.reserved_job_id not in (None, job.pk):
        raise ValidationError('Свободных муфт нет. Выберите, с какой запланированной работы снять резерв.')
    if not source and not number and not first.item.reserved_job_id:
        free_warehouses={r.warehouse_id for r in rows if not r.item.reserved_job_id}
        if len(free_warehouses)>1:
            raise ValidationError('Совместимые свободные муфты есть на нескольких складах. Укажите фактический складской источник.')
    if source and len(rows) != 1:
        raise ValidationError('У изделия несколько мест хранения. Требуется сверка складского остатка.')
    return first


def attach(actor, job, row, serial, existing=None):
    item, warehouse = row.item, row.warehouse
    if item.coupling_id and existing and item.coupling_id != existing.pk:
        placeholder=item.coupling
        if (placeholder.serial or not placeholder.source.get('stock_created') or
                item.movements.filter(kind='consume').exists() or CouplingUse.objects.filter(coupling=placeholder).exists()):
            raise ValidationError('Складская позиция уже связана с другой записью изделия. Требуется сверка связей.')
        item.coupling=None;item.save(update_fields=['coupling'])
        audit(actor,'stock_placeholder_merged',placeholder=str(placeholder.pk),coupling=str(existing.pk),item=str(item.pk))
        placeholder.delete()

    if item.coupling_id:
        coupling = item.coupling
    elif existing:
        coupling = existing
    else:
        coupling = Coupling(project_object=job.section.project_object, method=item.method,
            length_mm=item.length_mm, wall_mm=item.wall_mm, source={'stock_created': True})
    if StockItem.objects.filter(legal_entity=item.legal_entity, method=item.method, serial=serial,
            deleted_at__isnull=True).exclude(pk=item.pk).exists():
        raise ValidationError('Этот заводской номер уже связан с другой складской позицией.')
    if coupling.pk and CouplingUse.objects.filter(coupling=coupling).exclude(job=job).exists():
        raise ValidationError('Изделие уже смонтировано на другой работе.')
    # Old grouped stock is split through append-only postings, not an invented balance.
    if row.quantity != 1 or item.balances.filter(quantity__gt=0).count() != 1:
        if item.individual or item.coupling_id or item.serial:
            raise ValidationError('Неоднозначный остаток отдельного изделия. Требуется сверка.')
        values = {k: getattr(item, k) for k in stock.ITEM_FIELDS}
        values.update(sku='МОН-'+str(uuid.uuid4())[:12], coupling=None)
        piece = StockItem.objects.create(legal_entity=item.legal_entity, created_by=actor,
            individual=True, receipt_warehouse=item.receipt_warehouse, receipt_date=item.receipt_date,
            receipt_origin=item.receipt_origin, **values)
        note = 'Выделение изделия для монтажа из позиции '+str(item.pk)
        stock.post_locked(actor,item,'loss',Decimal(1),warehouse,None,timezone.localdate(),note,uuid.uuid4(),automatic=True)
        stock.post_locked(actor,piece,'receipt',Decimal(1),None,warehouse,timezone.localdate(),note,uuid.uuid4(),automatic=True)
        audit(actor,'stock_piece_split',item=str(item.pk),piece=str(piece.pk),warehouse=str(warehouse.pk))
        item = piece
    donor_id = item.reserved_job_id
    if donor_id and donor_id != job.pk:
        # The company stock lock is already held; bump donor version to invalidate stale forms.
        from .models import Job
        donor = Job.objects.select_for_update(no_key=True).get(pk=donor_id)
        if donor.state in ('reported','confirmed','unrepairable'):
            raise ValidationError('Прежняя работа уже закрыта. Обновите сведения о резерве.')
        donor.version += 1; donor.materials='unknown'; donor.save(update_fields=['version','materials'])
    coupling.project_object=job.section.project_object
    coupling.serial=serial; coupling.warehouse=warehouse.name
    coupling.full_clean(); coupling.save()
    item.coupling=coupling; item.serial=serial; item.individual=True
    item.reserved_job=job; item.reservation_origin='installation'; item.version+=1; item.save()
    audit(actor,'stock_installation_bound',item=str(item.pk),coupling=str(coupling.pk),
          job=str(job.pk),previous_job=str(donor_id or ''),warehouse=str(warehouse.pk))
    return coupling, {'item':str(item.pk),'coupling':str(coupling.pk),'warehouse':str(warehouse.pk),
                      'previous_job':str(donor_id or ''),'job':str(job.pk)}


def resolve(actor,job,piece,method,wall):
    from .coupling_fields import WholeWallField
    wall=WholeWallField(min_value=1,max_digits=4,decimal_places=0).clean(wall)
    serial=str(piece.get('serial','')).strip()
    length=piece.get('length_mm')
    if not serial or len(serial)>100 or not isinstance(length,int) or isinstance(length,bool) or length<=0:
        raise ValidationError('Для каждой муфты нужны фактическая длина и заводской номер.')
    existing=Coupling.objects.filter(project_object=job.section.project_object,method=method,serial=serial).first()
    if existing:
        if existing.length_mm!=length or existing.wall_mm!=wall:
            raise ValidationError('Заводской номер уже относится к изделию с другими размерами.')
        if CouplingUse.objects.filter(coupling=existing).exclude(job=job).exists():
            raise ValidationError('Муфта уже смонтирована на другой работе.')
        linked=StockItem.objects.filter(coupling=existing).first()
        if linked and stock.StockMovement.objects.filter(job=job,item=linked,kind='consume',reversal__isnull=True).exists():
            return existing, None
    row=choose(job,method,length,wall,serial,piece.get('source'))
    return attach(actor,job,row,serial,existing)
