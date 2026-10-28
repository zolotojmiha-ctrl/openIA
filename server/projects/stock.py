"""Append-only stock ledger. A company lock serializes postings and installation hooks."""
import uuid,hashlib,json
from decimal import Decimal,InvalidOperation
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction
from django.db.models import Q,Sum
from django.utils import timezone
from accounts.models import User
from accounts.permissions import permitted
from accounts.views import audit
from .models import Warehouse,WarehouseAccess,StockItem,StockBalance,StockMovement,StockEntry,StockLock,LegalEntity,COMPANY_TAX_ID
from .access import visible_objects

def warehouses(user):
    qs=Warehouse.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID)
    if not user.is_authenticated or not user.is_active:return qs.none()
    return qs if permitted(user,'stock.manage') else qs.filter(access_rules__user=user)

def items(user):
    qs=StockItem.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID,deleted_at__isnull=True)
    if not permitted(user,'stock.manage'):qs=qs.filter(balances__warehouse__in=warehouses(user)).distinct()
    return qs

def movements(user):
    qs=StockMovement.objects.filter(item__legal_entity__tax_id=COMPANY_TAX_ID)
    if not permitted(user,'stock.manage'):
        allowed=warehouses(user)
        # Both sides must be visible; a transfer never reveals another warehouse.
        qs=qs.filter(Q(source__isnull=True)|Q(source__in=allowed)).filter(Q(destination__isnull=True)|Q(destination__in=allowed))
    return qs.select_related('item','source','destination','created_by')

def lock():
    company=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
    row,_=StockLock.objects.get_or_create(legal_entity=company)
    StockLock.objects.select_for_update().get(pk=row.pk)
    return company

def manager(user):
    actor=User.objects.select_for_update(no_key=True).get(pk=user.pk)
    if not permitted(actor,'stock.manage') or actor.session_version!=user.session_version:raise PermissionDenied
    return actor

@transaction.atomic
def save_warehouse(user,pk,version,data):
    actor=manager(user);company=lock()
    row=warehouses(actor).get(pk=pk) if pk else Warehouse(legal_entity=company,created_by=actor)
    if pk and row.version!=version:raise ValidationError('Склад изменён. Обновите страницу.')
    multi='served_objects' in data or 'served_brigades' in data
    obj=row.project_object if multi else data.get('project_object');crew=data.get('brigade_assignment',row.brigade_assignment) if multi else data.get('brigade_assignment')
    if multi:
        from .crews import visible_crews
        areas=list(data.get('served_objects',[]));crews=list(data.get('served_brigades',[]))
        if set(o.pk for o in areas)-set(visible_objects(actor).exclude(status='archived').values_list('pk',flat=True)):raise ValidationError('Выбран недоступный участок.')
        if set(b.pk for b in crews)-set(visible_crews(actor).filter(archived=False).values_list('pk',flat=True)):raise ValidationError('Выбрана недоступная бригада.')
    if obj and not visible_objects(actor).filter(pk=obj.pk).exists():raise ValidationError('Объект недоступен.')
    if crew:
        from .crews import visible_assignments
        if not visible_assignments(actor).filter(pk=crew.pk).exists():raise ValidationError('Назначение бригады недоступно.')
        if obj and obj.pk!=crew.project_object_id:raise ValidationError('Объект не совпадает с назначением бригады.')
        obj=crew.project_object
    if pk and (row.project_object_id!=(obj.pk if obj else None) or row.brigade_assignment_id!=(crew.pk if crew else None)) and row.balances.filter(quantity__gt=0).exists():raise ValidationError('Склад с остатками нельзя переносить на другую бригаду или объект. Оформите перемещение.')
    if data.get('archived') and row.balances.filter(quantity__gt=0).exists():raise ValidationError('Нельзя архивировать склад с остатками.')
    before={'name':row.name,'location':row.location,'object':str(row.project_object_id or ''),'crew':str(row.brigade_assignment_id or ''),'archived':row.archived} if pk else None
    row.name=data['name'].strip();row.location=data.get('location','').strip();row.project_object=obj;row.brigade_assignment=crew;row.archived=data.get('archived',False)
    row.full_clean()
    if pk:row.version+=1
    row.save()
    if multi:
        row.served_objects.set(areas);row.served_brigades.set(crews)
        if crew:row.served_objects.add(crew.project_object_id);row.served_brigades.add(crew.brigade_id)
    else:
        if obj:row.served_objects.add(obj)
        if crew:row.served_brigades.add(crew.brigade_id)
    audit(actor,'stock_updated',kind='warehouse',id=str(row.pk),previous=before,name=row.name,location=row.location,object=str(row.project_object_id or ''),crew=str(row.brigade_assignment_id or ''),archived=row.archived,served_objects=[str(x) for x in row.served_objects.values_list('pk',flat=True)],served_brigades=[str(x) for x in row.served_brigades.values_list('pk',flat=True)])
    return row

ITEM_FIELDS=('sku','name','kind','unit','method','diameter_mm','length_mm','wall_mm','archived')
@transaction.atomic
def save_item(user,pk,version,data):
    from repairs.models import Coupling,CouplingUse
    actor=manager(user);company=lock()
    row=items(actor).get(pk=pk) if pk else StockItem(legal_entity=company,created_by=actor)
    if pk and row.version!=version:raise ValidationError('Номенклатура изменена. Обновите страницу.')
    coupling=data.get('coupling')
    if pk and row.coupling_id!=(coupling.pk if coupling else None):raise ValidationError('Связь с конкретной муфтой не переписывается. Создайте другую позицию.')
    before={k:str(getattr(row,k)) for k in (*ITEM_FIELDS,'serial','receipt_origin')} if pk else None
    for k in ITEM_FIELDS:setattr(row,k,data.get(k))
    row.method=row.method or ''
    if 'serial' in data:row.serial=data['serial'].strip()
    if 'receipt_origin' in data:row.receipt_origin=(data['receipt_origin'] or '').strip()
    if 'electrode_brand' in data:row.electrode_brand=(data['electrode_brand'] or '').strip()
    if 'electrode_diameter' in data:row.electrode_diameter=data['electrode_diameter']
    if row.kind=='electrode' and (row.unit!='kg' or not row.electrode_brand or not row.electrode_diameter or row.electrode_diameter<=0):raise ValidationError('Для электродов нужны марка, диаметр и единица кг.')
    if row.kind in ('composite','sealant') and row.unit!='pcs':raise ValidationError('Композит и герметик учитываются в штуках.')
    if row.kind=='insulation' and row.unit!='m2':raise ValidationError('Изоляция учитывается в квадратных метрах (м²).')
    if row.coupling_id and row.serial!=row.coupling.serial and 'serial' in data:
        row.coupling.serial=row.serial;row.coupling.full_clean();row.coupling.save(update_fields=['serial'])
    if coupling:
        coupling=Coupling.objects.select_for_update().get(pk=coupling.pk)
        if not visible_objects(actor).filter(pk=coupling.project_object_id).exists():raise ValidationError('Объект муфты недоступен.')
        if not pk and (CouplingUse.objects.filter(coupling=coupling).exists() or coupling.source.get('historical_installation')):raise ValidationError('Уже установленная или историческая муфта не принимается в новый складской остаток.')
        if coupling.wall_mm is not None and coupling.wall_mm!=int(coupling.wall_mm):raise ValidationError('Толщина стенки конкретной муфты должна быть целой.')
        row.coupling=coupling;row.kind='coupling';row.unit='pcs';row.method=coupling.method;row.length_mm=coupling.length_mm;row.wall_mm=int(coupling.wall_mm) if coupling.wall_mm is not None else None
    if row.kind=='coupling':
        if row.unit!='pcs' or row.method not in ('p1','p2','p3','p4','p5u') or not row.length_mm or not row.wall_mm:raise ValidationError('Для муфты нужны метод П1–П5У, длина и целая толщина стенки; единица — шт.')
    if pk and row.movements.exists():
        previous=StockItem.objects.get(pk=pk)
        if any(getattr(previous,k)!=getattr(row,k) for k in ('kind','unit','method','diameter_mm','length_mm','wall_mm','electrode_brand','electrode_diameter')):raise ValidationError('Размеры и единицы позиции с историей не меняются. Создайте другую номенклатуру.')
    if row.archived and row.balances.filter(quantity__gt=0).exists():raise ValidationError('Номенклатуру с остатками нельзя архивировать.')
    if row.kind=='coupling' and row.serial and pk and row.serial!=before['serial'] and not row.individual and not row.coupling_id and (row.balances.aggregate(q=Sum('quantity'))['q'] or 0)>1:raise ValidationError('Заводской номер относится к одному изделию. Сначала выделите отдельную муфту или заполните номера отдельными строками Excel.')
    if row.serial and (not pk or row.serial!=before['serial']) and StockItem.objects.filter(legal_entity=company,kind='coupling',method=row.method,serial=row.serial,deleted_at__isnull=True).exclude(pk=row.pk).exists():raise ValidationError('Муфта такого типа с этим заводским номером уже заведена.')
    row.full_clean()
    if pk:row.version+=1
    row.save();audit(actor,'stock_updated',kind='item',id=str(row.pk),previous=before,values={k:str(getattr(row,k)) for k in (*ITEM_FIELDS,'serial','receipt_origin')},coupling=str(row.coupling_id or ''))
    return row

def quantity(raw,item):
    try:q=Decimal(str(raw))
    except InvalidOperation:raise ValidationError('Укажите количество числом.')
    if not q.is_finite() or q<=0 or q>=Decimal('1000000000000000') or q.normalize().as_tuple().exponent < -3:raise ValidationError('Количество должно быть положительным, до трёх знаков после запятой.')
    if item.unit=='pcs' and q!=q.to_integral_value():raise ValidationError('Штучные изделия учитываются целым количеством.')
    if (item.coupling_id or item.individual) and q!=1:raise ValidationError('Конкретная муфта учитывается как одно изделие.')
    return q

def post_locked(actor,item,kind,q,source,destination,date,note,token,job=None,report=None,reverses=None,automatic=False):
    payload={'item':str(item.pk),'kind':kind,'quantity':str(q.normalize()),'source':str(source.pk) if source else '', 'destination':str(destination.pk) if destination else '', 'date':str(date),'note':note,'job':str(job.pk) if job else '', 'report':str(report.pk) if report else '', 'reverses':str(reverses.pk) if reverses else ''}
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    old=StockMovement.objects.filter(token=token).first()
    if old:
        if old.digest!=digest or old.created_by_id!=actor.pk:raise ValidationError('Этот запрос уже сохранён с другими данными.')
        return old
    if item.archived:raise ValidationError('Номенклатура в архиве. Сначала восстановите карточку.')
    if not note.strip() or len(note)>1500:raise ValidationError('Укажите основание операции до 1500 символов.')
    if not date or date>timezone.localdate():raise ValidationError('Складская операция проводится фактической датой, не в будущем.')
    legs=[]
    if source:legs.append((source,-q))
    if destination:legs.append((destination,q))
    for wh,delta in legs:
        balance,_=StockBalance.objects.get_or_create(warehouse=wh,item=item)
        if balance.quantity+delta>=Decimal('1000000000000000'):raise ValidationError('Остаток превышает допустимую величину.')
        if balance.quantity+delta<0:raise ValidationError('Недостаточный остаток. Операция не сохранена.')
        if wh.archived:raise ValidationError('Склад находится в архиве.')
    if item.coupling_id or item.individual:
        total=StockBalance.objects.filter(item=item).aggregate(q=Sum('quantity'))['q'] or Decimal(0)
        if total+sum(delta for wh,delta in legs)>1:raise ValidationError('Эта конкретная муфта уже учтена на складе или у бригады.')
    row=StockMovement.objects.create(token=token,digest=digest,item=item,kind=kind,quantity=q,source=source,destination=destination,date=date,note=note,job=job,report=report,reverses=reverses,automatic=automatic,created_by=actor)
    for wh,delta in legs:
        balance=StockBalance.objects.get(warehouse=wh,item=item);balance.quantity+=delta;balance.save(update_fields=['quantity']);StockEntry.objects.create(movement=row,warehouse=wh,quantity=delta)
    audit(actor,'stock_moved',movement=str(row.pk),**payload,automatic=automatic)
    return row

@transaction.atomic
def move(user,item_id,kind,raw,source_id,destination_id,date,note,token,job_id=None):
    from repairs.models import Coupling,CouplingUse
    from repairs.access import visible_jobs
    actor=manager(user);lock();item=items(actor).get(pk=item_id);q=quantity(raw,item)
    old=StockMovement.objects.filter(token=token).first()
    source=warehouses(actor).get(pk=source_id) if source_id else None;destination=warehouses(actor).get(pk=destination_id) if destination_id else None
    job=visible_jobs(actor).get(pk=job_id) if job_id else None
    if old:return post_locked(actor,item,kind,q,source,destination,date,note,token,job)
    if item.archived:raise ValidationError('Номенклатура в архиве.')
    if not date or date>timezone.localdate():raise ValidationError('Укажите фактическую дату, не в будущем.')
    if item.reserved_job_id and (kind in ('consume','loss') or job and job.pk!=item.reserved_job_id):raise ValidationError('Муфта закреплена за другой работой либо зарезервирована. Сначала снимите назначение.')
    if item.coupling_id and kind=='consume':raise ValidationError('Конкретная муфта списывается автоматически при сохранении результата установки.')
    if kind=='receipt':valid=not source and destination and not destination.brigade_assignment_id
    elif kind=='transfer':valid=source and destination and not source.brigade_assignment_id and not destination.brigade_assignment_id
    elif kind=='issue':valid=source and destination and not source.brigade_assignment_id and destination.brigade_assignment_id
    elif kind=='return':valid=source and destination and source.brigade_assignment_id and not destination.brigade_assignment_id
    elif kind=='consume':valid=source and not destination and job
    elif kind=='loss':valid=source and not destination
    else:valid=False
    if not valid or source and destination and source.pk==destination.pk:raise ValidationError('Проверьте вид операции, склад отправления и получателя. Для списания на работу выберите работу.')
    if source and source.brigade_assignment_id and kind=='consume' and source.project_object_id!=job.section.project_object_id:raise ValidationError('Бригада и работа относятся к разным объектам.')
    if destination and destination.brigade_assignment_id:
        from .crews import visible_assignments
        crew=visible_assignments(actor).select_for_update(of=('self',)).filter(pk=destination.brigade_assignment_id).first()
        if not crew or date<crew.start_date or crew.end_date and date>crew.end_date:raise ValidationError('Назначение бригады недоступно или не действует на дату выдачи.')
    if kind=='issue' and job and (job.section.project_object_id!=destination.project_object_id or job.assigned_to_id!=destination.brigade_assignment.master_id or job.crew_assignment_id and job.crew_assignment_id!=destination.brigade_assignment_id):raise ValidationError('Выдача на работу должна соответствовать объекту и ответственному мастеру / бригаде.')
    if item.coupling_id:
        coupling=Coupling.objects.select_for_update().get(pk=item.coupling_id)
        if kind=='issue' and job and coupling.method!=job.method:raise ValidationError('Метод ремонта не соответствует выданной муфте.')
        if CouplingUse.objects.filter(coupling=coupling).exists():raise ValidationError('Установленная муфта не перемещается и не списывается повторно. Исправьте результат ремонта.')
        target=destination.project_object_id if destination else None
        if item.reserved_job_id and target and target!=item.reserved_job.section.project_object_id:raise ValidationError('Муфта закреплена за работой другого участка. Сначала снимите назначение.')
        if target and target!=coupling.project_object_id:
            if not visible_objects(actor).filter(pk=target).exists():raise ValidationError('Объект получателя недоступен.')
            coupling.project_object_id=target;coupling.full_clean();coupling.save(update_fields=['project_object'])
    return post_locked(actor,item,kind,q,source,destination,date,note,token,job)

@transaction.atomic
def reverse(user,pk,note,token):
    actor=manager(user);lock();row=movements(actor).get(pk=pk)
    previous=StockMovement.objects.filter(token=token).first()
    if previous:
        if previous.reverses_id==row.pk and previous.created_by_id==actor.pk and previous.note==note:return previous
        raise ValidationError('Этот запрос уже использован.')
    if row.item.reserved_job_id:raise ValidationError('Сначала снимите закрепление муфты за работой.')
    if row.automatic:raise ValidationError('Автоматическое списание меняется через исправление результата ремонта.')
    if row.reverses_id or StockMovement.objects.filter(reverses=row).exists():raise ValidationError('Операция уже отменена либо сама является отменой.')
    if row.item.coupling_id:
        from repairs.models import CouplingUse
        if CouplingUse.objects.filter(coupling_id=row.item.coupling_id).exists():raise ValidationError('Муфта установлена. Сначала исправьте результат ремонта.')
        if row.kind in ('issue','transfer','return'):raise ValidationError('Перемещение конкретной муфты возвращайте отдельной обратной операцией, чтобы сохранить её привязку к объекту.')
    return post_locked(actor,row.item,'reversal',row.quantity,row.destination,row.source,timezone.localdate(),note,token,reverses=row)

def reconcile_installation(actor,job,report,selected):
    """Called under StockLock and coupling locks, inside the repair transaction."""
    selected_ids={c.pk for c in selected}
    active=StockMovement.objects.filter(job=job,automatic=True,kind='consume',reversal__isnull=True).select_related('item','source')
    kept=set()
    for row in active:
        if row.item.coupling_id in selected_ids:kept.add(row.item.coupling_id);continue
        post_locked(actor,row.item,'reversal',row.quantity,None,row.source,timezone.localdate(),'Исправлен состав установленных муфт: '+str(report.pk),uuid.uuid5(report.token,'reverse:'+str(row.pk)),job,report,reverses=row,automatic=True)
    for item in StockItem.objects.filter(coupling_id__in=selected_ids-kept):
        balance=StockBalance.objects.filter(item=item,quantity=1).select_related('warehouse','warehouse__brigade_assignment').first()
        if not balance:raise ValidationError('У выбранной муфты нет складского остатка. Проверьте поступление и выдачу бригаде.')
        wh=balance.warehouse
        if item.reserved_job_id and item.reserved_job_id!=job.pk:raise ValidationError('Муфта закреплена за другой работой.')
        if item.reserved_job_id:
            from .stock_planning import requirements
            lengths=requirements(job)
            if lengths is None or item.method!=job.method or item.wall_mm!=job.wall_mm or item.length_mm not in lengths or job.section.diameter_mm and item.diameter_mm!=job.section.diameter_mm:raise ValidationError('Характеристики работы изменились после резервирования. Проверьте закреплённые муфты.')
        latest=StockMovement.objects.filter(item=item).first()
        if latest and latest.kind=='issue' and latest.job_id and latest.job_id!=job.pk:raise ValidationError('Муфта выдана на другую работу. Уточните назначение материала.')
        if not actor.is_leader and item.reserved_job_id!=job.pk and (wh.brigade_assignment_id!=job.crew_assignment_id or not wh.brigade_assignment_id) and wh.project_object_id!=job.section.project_object_id:raise ValidationError('Сначала выдайте муфту бригаде или на склад этого объекта.')
        if wh.brigade_assignment_id and wh.project_object_id!=job.section.project_object_id:raise ValidationError('Муфта числится у бригады другого объекта.')
        post_locked(actor,item,'consume',Decimal(1),wh,None,report.performed_date,'Установка муфты по результату '+str(report.pk),uuid.uuid5(report.token,'consume:'+str(item.pk)),job,report,automatic=True)


def prepared_couplings(job):
    """Only an explicit issue to this job prepares a coupling for the simple form."""
    result=[]
    for item in StockItem.objects.filter(coupling__project_object=job.section.project_object,archived=False,balances__quantity=1).select_related('coupling').distinct():
        latest=StockMovement.objects.filter(item=item).first()
        if item.reserved_job_id==job.pk or latest and latest.kind=='issue' and latest.job_id==job.pk:result.append(item.coupling)
    return result
