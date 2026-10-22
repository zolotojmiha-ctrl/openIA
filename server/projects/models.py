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

class Contract(models.Model):
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

class ContractObject(models.Model):
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

class BrigadeAssignment(models.Model):
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

class EquipmentAssignment(models.Model):
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
