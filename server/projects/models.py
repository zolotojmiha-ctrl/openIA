import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

COMPANY_TAX_ID = '8604073687'
STATUSES = [('planned','Подготовка'),('active','В работе'),('completed','Завершён'),('archived','В архиве')]
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
    counterparty = models.CharField('Контрагент', max_length=200, blank=True)
    deadline = models.DateField('Срок окончания', null=True, blank=True)
    amount = models.DecimalField('Сумма по карточке, ₽', max_digits=18, decimal_places=2, null=True, blank=True)
    private_notes = models.TextField('Внутренние примечания — только руководителям', max_length=5000, blank=True)
    status = models.CharField('Состояние', max_length=20, choices=STATUSES, default='planned')
    objects_linked = models.ManyToManyField(ProjectObject, through='ContractObject', related_name='contracts')
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    def __str__(self):
        return self.number
    def clean(self):
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
