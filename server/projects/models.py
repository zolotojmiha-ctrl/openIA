from projects.archive import ArchiveProtectedModel
import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

COMPANY_TAX_ID = '8604073687'
STATUSES = [('planned','Подготовка'),('active','В работе'),('completed','Завершён'),('archived','В архиве')]
WORK_KINDS=[('repairs','Устранение дефектов'),('valves','Замена запорной арматуры'),('diagnostics','Подготовка к диагностике'),('construction','Другие СМР')]

SHARED_FIELDS = {
    'show_number':'Номер договора',
    'show_date':'Дата договора',
    'show_subject':'Предмет договора',
    'show_deadline':'Срок окончания',
    'show_counterparty':'Контрагент',
}

class LegalEntity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    tax_id = models.CharField(max_length=12, unique=True)
    def __str__(self):
        return self.name

class ProjectObject(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    legal_entity = models.ForeignKey(LegalEntity, on_delete=models.PROTECT)
    name = models.CharField('Название', max_length=200)
    kind = models.CharField('Вид объекта', max_length=120, blank=True)
    location = models.CharField('Местоположение', max_length=250, blank=True)
    status = models.CharField('Состояние', max_length=20, choices=STATUSES, default='planned')
    start_date = models.DateField('Начало работ', null=True, blank=True)
    end_date = models.DateField('Окончание работ', null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    def __str__(self):
        return self.name
    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({'end_date':'Окончание не может быть раньше начала.'})

class Contract(ArchiveProtectedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    legal_entity = models.ForeignKey(LegalEntity, on_delete=models.PROTECT)
    number = models.CharField('Номер договора', max_length=100)
    date = models.DateField('Дата', null=True, blank=True)
    subject = models.CharField('Предмет договора', max_length=300)
    work_kinds = models.JSONField('Виды работ', default=list, blank=True)
    counterparty = models.CharField('Контрагент', max_length=200, blank=True)
    deadline = models.DateField('Срок окончания', null=True, blank=True)
    amount = models.DecimalField('Сумма по карточке, ₽', max_digits=18, decimal_places=2, null=True, blank=True)
    private_notes = models.TextField('Внутренние примечания — только руководителям', max_length=5000, blank=True)
    status = models.CharField('Состояние', max_length=20, choices=STATUSES, default='planned')
    objects_linked = models.ManyToManyField(ProjectObject, through='ContractObject', related_name='contracts')
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    @property
    def work_kinds_display(self):
        return ', '.join(dict(WORK_KINDS)[k] for k in self.work_kinds if k in dict(WORK_KINDS)) or 'Виды работ не выбраны'
    def __str__(self):
        return self.number
    def clean(self):
        if not isinstance(self.work_kinds,list) or any(k not in dict(WORK_KINDS) for k in self.work_kinds) or len(set(self.work_kinds))!=len(self.work_kinds):
            raise ValidationError({'work_kinds':'Выберите виды работ из списка.'})
        if self.amount is not None and self.amount < 0:
            raise ValidationError({'amount':'Сумма не может быть отрицательной.'})
        if self.date and self.deadline and self.deadline < self.date:
            raise ValidationError({'deadline':'Срок окончания не может быть раньше даты договора.'})

class ContractObject(ArchiveProtectedModel):
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT)
    project_object = models.ForeignKey(ProjectObject, on_delete=models.PROTECT)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['contract','project_object'],name='unique_contract_object')]
    def clean(self):
        if self.contract.legal_entity_id != self.project_object.legal_entity_id:
            raise ValidationError('Нельзя связывать записи разных юридических лиц.')
    def save(self,*args,**kwargs):
        self.clean()
        return super().save(*args,**kwargs)

class ObjectAccess(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='object_assignments')
    project_object = models.ForeignKey(ProjectObject, on_delete=models.PROTECT, related_name='assignments')
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['user','project_object'],name='unique_user_object')]

class ContractAccess(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='contract_access')
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name='access_rules')
    show_number = models.BooleanField(default=False)
    show_date = models.BooleanField(default=False)
    show_subject = models.BooleanField(default=False)
    show_deadline = models.BooleanField(default=False)
    show_counterparty = models.BooleanField(default=False)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['user','contract'],name='unique_user_contract')]

class Brigade(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    legal_entity=models.ForeignKey(LegalEntity,on_delete=models.PROTECT)
    name=models.CharField('Название бригады',max_length=160)
    foreman=models.ForeignKey('documents.Person',on_delete=models.PROTECT,related_name='led_brigades',verbose_name='Мастер')
    archived=models.BooleanField(default=False)
    version=models.PositiveIntegerField(default=1)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    def __str__(self):return self.name

class BrigadeMember(models.Model):
    brigade=models.ForeignKey(Brigade,on_delete=models.PROTECT,related_name='memberships')
    person=models.ForeignKey('documents.Person',on_delete=models.PROTECT,related_name='brigade_memberships')
    start_date=models.DateField('Начало участия')
    end_date=models.DateField('Окончание участия',null=True,blank=True)
    version=models.PositiveIntegerField(default=1)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    class Meta:
        constraints=[models.CheckConstraint(condition=models.Q(end_date__isnull=True)|models.Q(end_date__gte=models.F('start_date')),name='brigade_member_dates')]

class BrigadeAssignment(ArchiveProtectedModel):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    brigade=models.ForeignKey(Brigade,on_delete=models.PROTECT,related_name='assignments')
    contract=models.ForeignKey(Contract,on_delete=models.PROTECT,related_name='brigade_assignments')
    project_object=models.ForeignKey(ProjectObject,on_delete=models.PROTECT,related_name='brigade_assignments')
    master=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='crew_assignments')
    start_date=models.DateField('Начало работы')
    end_date=models.DateField('Окончание работы',null=True,blank=True)
    roster=models.JSONField(default=list)
    version=models.PositiveIntegerField(default=1)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    class Meta:
        constraints=[models.CheckConstraint(condition=models.Q(end_date__isnull=True)|models.Q(end_date__gte=models.F('start_date')),name='brigade_assignment_dates')]
    def __str__(self):return f'{self.brigade.name} · {self.project_object.name} · с {self.start_date:%d.%m.%Y}'

class CrewImportReceipt(models.Model):
    token=models.UUIDField(unique=True)
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at=models.DateTimeField(auto_now_add=True)

EQUIPMENT_STATES=[('ready','Готова к работе'),('working','В работе'),('repair','В ремонте'),('reserve','В резерве'),('archived','В архиве')]
class Equipment(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    legal_entity=models.ForeignKey(LegalEntity,on_delete=models.PROTECT)
    name=models.CharField('Название / модель',max_length=200)
    category=models.CharField('Вид техники',max_length=100)
    inventory_number=models.CharField('Инвентарный номер',max_length=80)
    registration_number=models.CharField('Госномер',max_length=80,blank=True)
    serial_number=models.CharField('Заводской номер / VIN',max_length=100,blank=True)
    ownership=models.CharField('Собственность / аренда / субподряд',max_length=20,choices=[('own','Собственная'),('rent','Арендованная'),('subcontractor','Субподрядчика')],default='own')
    owner=models.CharField('Собственник / арендодатель',max_length=200,blank=True)
    status=models.CharField('Состояние',max_length=20,choices=EQUIPMENT_STATES,default='ready')
    notes=models.TextField('Примечание',max_length=3000,blank=True)
    version=models.PositiveIntegerField(default=1)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['legal_entity','inventory_number'],name='equipment_inventory_unique')]
    def __str__(self):return f'{self.name} · {self.inventory_number}'

class EquipmentAssignment(ArchiveProtectedModel):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    equipment=models.ForeignKey(Equipment,on_delete=models.PROTECT,related_name='placements')
    project_object=models.ForeignKey(ProjectObject,on_delete=models.PROTECT,related_name='equipment_placements')
    contract=models.ForeignKey(Contract,on_delete=models.PROTECT,null=True,blank=True,related_name='equipment_placements')
    start_date=models.DateField('Начало работы')
    end_date=models.DateField('Окончание работы',null=True,blank=True)
    note=models.CharField('Пояснение',max_length=1000,blank=True)
    version=models.PositiveIntegerField(default=1)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    class Meta:
        constraints=[models.CheckConstraint(condition=models.Q(end_date__isnull=True)|models.Q(end_date__gte=models.F('start_date')),name='equipment_placement_dates')]

class EquipmentDocument(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    equipment=models.ForeignKey(Equipment,on_delete=models.PROTECT,related_name='file_links')
    document=models.ForeignKey('documents.Document',on_delete=models.PROTECT,related_name='equipment_links')
    label=models.CharField('Вид документа',max_length=120)
    expires_on=models.DateField('Действует до',null=True,blank=True)
    remind=models.BooleanField('Напоминать об окончании',default=False)
    days_before=models.PositiveSmallIntegerField('За сколько дней',default=30)
    active=models.BooleanField(default=True)
    version=models.PositiveIntegerField(default=1)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['equipment','document'],name='equipment_document_unique')]

class EquipmentImportReceipt(models.Model):
    token=models.UUIDField(unique=True)
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at=models.DateTimeField(auto_now_add=True)

class StockLock(models.Model):
    legal_entity=models.OneToOneField(LegalEntity,on_delete=models.PROTECT)

class Warehouse(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    legal_entity=models.ForeignKey(LegalEntity,on_delete=models.PROTECT)
    name=models.CharField('Название склада',max_length=160)
    location=models.CharField('Место хранения / адрес',max_length=250,blank=True)
    project_object=models.ForeignKey(ProjectObject,on_delete=models.PROTECT,null=True,blank=True,verbose_name='Объект (необязательно)')
    brigade_assignment=models.OneToOneField(BrigadeAssignment,on_delete=models.PROTECT,null=True,blank=True,verbose_name='Выдано бригаде (назначение)')
    archived=models.BooleanField('В архиве',default=False)
    version=models.PositiveIntegerField(default=1)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['legal_entity','name'],name='warehouse_name_unique')]
    def __str__(self):return self.name

class WarehouseAccess(models.Model):
    warehouse=models.ForeignKey(Warehouse,on_delete=models.PROTECT,related_name='access_rules')
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    granted_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['warehouse','user'],name='warehouse_user_unique')]

class StockItem(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    legal_entity=models.ForeignKey(LegalEntity,on_delete=models.PROTECT)
    sku=models.CharField('Код номенклатуры',max_length=80)
    name=models.CharField('Наименование',max_length=200)
    kind=models.CharField('Вид',max_length=20,choices=[('material','Материал'),('coupling','Муфта')],default='material')
    unit=models.CharField('Единица',max_length=10,choices=[('pcs','шт.'),('m','м'),('kg','кг'),('l','л')],default='pcs')
    method=models.CharField('Метод ремонта / тип муфты',max_length=20,blank=True)
    diameter_mm=models.PositiveIntegerField('Диаметр, мм',null=True,blank=True)
    length_mm=models.PositiveIntegerField('Длина, мм',null=True,blank=True)
    wall_mm=models.PositiveIntegerField('Толщина стенки, мм',null=True,blank=True)
    coupling=models.OneToOneField('repairs.Coupling',on_delete=models.PROTECT,null=True,blank=True,related_name='stock_item',verbose_name='Конкретная муфта из реестра ремонтов')
    archived=models.BooleanField('В архиве',default=False)
    version=models.PositiveIntegerField(default=1)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['legal_entity','sku'],name='stock_sku_unique')]
    @property
    def method_label(self):
        return {'p1':'П1','p2':'П2','p3':'П3','p4':'П4','p5u':'П5У'}.get(self.method,self.method)
    def __str__(self):return f'{self.sku} · {self.name}'

class StockBalance(models.Model):
    warehouse=models.ForeignKey(Warehouse,on_delete=models.PROTECT,related_name='balances')
    item=models.ForeignKey(StockItem,on_delete=models.PROTECT,related_name='balances')
    quantity=models.DecimalField(max_digits=18,decimal_places=3,default=0)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['warehouse','item'],name='stock_balance_unique'),models.CheckConstraint(condition=models.Q(quantity__gte=0),name='stock_nonnegative')]

STOCK_KINDS=[('receipt','Поступление'),('transfer','Перемещение'),('issue','Выдача бригаде'),('return','Возврат от бригады'),('consume','Списание на работу'),('loss','Списание / недостача'),('reversal','Отмена операции')]
class StockMovement(ArchiveProtectedModel):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    token=models.UUIDField(unique=True,default=uuid.uuid4)
    digest=models.CharField(max_length=64)
    item=models.ForeignKey(StockItem,on_delete=models.PROTECT,related_name='movements')
    kind=models.CharField(max_length=20,choices=STOCK_KINDS)
    quantity=models.DecimalField(max_digits=18,decimal_places=3)
    source=models.ForeignKey(Warehouse,on_delete=models.PROTECT,null=True,blank=True,related_name='outgoing')
    destination=models.ForeignKey(Warehouse,on_delete=models.PROTECT,null=True,blank=True,related_name='incoming')
    job=models.ForeignKey('repairs.Job',on_delete=models.PROTECT,null=True,blank=True,related_name='stock_movements')
    report=models.ForeignKey('repairs.Report',on_delete=models.PROTECT,null=True,blank=True,related_name='stock_movements')
    date=models.DateField('Дата операции')
    note=models.CharField('Основание / пояснение',max_length=1500)
    reverses=models.OneToOneField('self',on_delete=models.PROTECT,null=True,blank=True,related_name='reversal')
    automatic=models.BooleanField(default=False)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=['-created_at','-pk']
        constraints=[models.CheckConstraint(condition=models.Q(quantity__gt=0),name='stock_movement_positive')]
    def save(self,*args,**kwargs):
        if not self._state.adding:raise ValueError('Складские операции не переписываются. Используйте отмену.')
        return super().save(*args,**kwargs)

class StockEntry(ArchiveProtectedModel):
    movement=models.ForeignKey(StockMovement,on_delete=models.PROTECT,related_name='entries')
    warehouse=models.ForeignKey(Warehouse,on_delete=models.PROTECT)
    quantity=models.DecimalField(max_digits=18,decimal_places=3)
    def save(self,*args,**kwargs):
        if not self._state.adding:raise ValueError('Проводки не переписываются.')
        return super().save(*args,**kwargs)

class StockImportReceipt(models.Model):
    token=models.UUIDField(unique=True)
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)

class Subcontractor(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    legal_entity=models.ForeignKey(LegalEntity,on_delete=models.PROTECT)
    name=models.CharField('Компания',max_length=200)
    tax_id=models.CharField('ИНН',max_length=12)
    contact=models.CharField('Контактное лицо и связь',max_length=250,blank=True)
    notes=models.TextField('Примечание',max_length=3000,blank=True)
    archived=models.BooleanField('В архиве',default=False)
    version=models.PositiveIntegerField(default=1)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['legal_entity','tax_id'],name='subcontractor_tax_unique')]
    def __str__(self):return self.name

class SubcontractRepresentative(models.Model):
    company=models.ForeignKey(Subcontractor,on_delete=models.PROTECT,related_name='representatives')
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='subcontract_memberships')
    active=models.BooleanField('Действует',default=True)
    version=models.PositiveIntegerField(default=1)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['company','user'],name='subcontract_rep_unique')]
    def __str__(self):return self.user.full_name

class SubcontractOrder(ArchiveProtectedModel):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    company=models.ForeignKey(Subcontractor,on_delete=models.PROTECT,related_name='orders')
    contract=models.ForeignKey(Contract,on_delete=models.PROTECT,related_name='subcontract_orders')
    project_object=models.ForeignKey(ProjectObject,on_delete=models.PROTECT,related_name='subcontract_orders')
    kind=models.CharField('Вид работ',max_length=20,choices=WORK_KINDS)
    title=models.CharField('Порученные работы',max_length=250)
    scope=models.TextField('Состав / границы поручения',max_length=3000,blank=True)
    planned_quantity=models.DecimalField('Плановое количество',max_digits=12,decimal_places=3,null=True,blank=True)
    unit=models.CharField('Единица измерения',max_length=30,blank=True)
    responsible=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name='subcontract_orders',verbose_name='Ответственный от нашей компании')
    state=models.CharField(max_length=20,choices=[('planned','Ещё не выполняли'),('progress','В работе / на доработке'),('problem','Есть препятствие'),('reported','Выполнено — на проверке'),('confirmed','Выполнено — проверено')],default='planned')
    archived=models.BooleanField('В архиве',default=False)
    version=models.PositiveIntegerField(default=1)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    @property
    def work_status(self):
        if self.kind!='repairs':return self.get_state_display()
        states=list(self.repair_jobs.filter(defects__archived_at__isnull=True).values_list('state',flat=True).distinct())
        if not states:return 'Дефекты ещё не выбраны'
        if all(s=='confirmed' for s in states):return 'Устранены — проверены'
        if 'reported' in states:return 'Есть устранённые — на проверке'
        if self.repair_jobs.filter(defects__archived_at__isnull=True).exclude(problem='').exists():return 'Есть препятствия'
        return 'В работе' if any(s!='planned' for s in states) else 'Ещё не выполняли'
    def __str__(self):return self.title

class SubcontractOrderAccess(models.Model):
    order=models.ForeignKey(SubcontractOrder,on_delete=models.PROTECT,related_name='access_rules')
    representative=models.ForeignKey(SubcontractRepresentative,on_delete=models.PROTECT,related_name='order_grants')
    active=models.BooleanField('Просмотр поручения',default=True)
    can_report=models.BooleanField('Вносить результат',default=False)
    version=models.PositiveIntegerField(default=1)
    granted_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['order','representative'],name='subcontract_order_rep_unique')]

class SubcontractDocument(ArchiveProtectedModel):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    company=models.ForeignKey(Subcontractor,on_delete=models.PROTECT,related_name='file_links')
    order=models.ForeignKey(SubcontractOrder,on_delete=models.PROTECT,null=True,blank=True,related_name='file_links')
    document=models.ForeignKey('documents.Document',on_delete=models.PROTECT,related_name='subcontract_links')
    label=models.CharField('Назначение документа',max_length=160)
    active=models.BooleanField('Показывать связь',default=True)
    version=models.PositiveIntegerField(default=1)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')

class SubcontractResult(ArchiveProtectedModel):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    order=models.ForeignKey(SubcontractOrder,on_delete=models.PROTECT,related_name='results')
    token=models.UUIDField(default=uuid.uuid4,unique=True)
    digest=models.CharField(max_length=64)
    outcome=models.CharField(max_length=20,choices=[('complete','Выполнено'),('partial','Выполнено частично'),('problem','Есть препятствие')])
    quantity=models.DecimalField('Выполнено с начала работ',max_digits=12,decimal_places=3,null=True,blank=True)
    date=models.DateField('Дата')
    note=models.TextField('Что выполнено / причина препятствия',max_length=3000)
    correction=models.BooleanField(default=False)
    author=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:ordering=['-created_at','-pk']

class SubcontractAttachment(ArchiveProtectedModel):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    result=models.ForeignKey(SubcontractResult,on_delete=models.PROTECT,related_name='attachments')
    storage_key=models.CharField(max_length=40)
    name=models.CharField(max_length=200)
    content_type=models.CharField(max_length=100)
    size=models.PositiveIntegerField()
    sha256=models.CharField(max_length=64)

class SubcontractDecision(ArchiveProtectedModel):
    result=models.OneToOneField(SubcontractResult,on_delete=models.PROTECT,related_name='decision')
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    decision=models.CharField(max_length=20,choices=[('confirmed','Подтверждено'),('revision','Вернули на доработку')])
    note=models.CharField(max_length=3000,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)

class SubcontractImportReceipt(models.Model):
    token=models.UUIDField(unique=True)
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)


class ContractClosureEvent(models.Model):
    contract=models.ForeignKey(Contract,on_delete=models.PROTECT,related_name='closure_events')
    action=models.CharField(max_length=20,choices=[('request','Заявка на закрытие'),('return','Возвращено на доработку'),('archive','Закрыт и отправлен в архив'),('restore','Восстановлен из архива')])
    note=models.TextField(max_length=5000)
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    previous_status=models.CharField(max_length=20)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=['-created_at','-pk']


class RegistryImportReceipt(models.Model):
    token=models.UUIDField(unique=True)
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    kind=models.CharField(max_length=20,choices=[('objects','Объекты'),('contracts','Контракты'),('people','Персонал')])
    created_at=models.DateTimeField(auto_now_add=True)
