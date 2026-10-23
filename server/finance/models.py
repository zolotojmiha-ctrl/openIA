from projects.archive import ArchiveProtectedModel
import uuid
from django.conf import settings
from django.db import models
from django.core.serializers.json import DjangoJSONEncoder

class FinancialScope(ArchiveProtectedModel):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    legal_entity=models.ForeignKey('projects.LegalEntity',on_delete=models.PROTECT)
    title=models.CharField('Направление / договор',max_length=200)
    revision=models.PositiveIntegerField(default=1)
    contract=models.ForeignKey('projects.Contract',on_delete=models.PROTECT,null=True,blank=True,related_name='financial_scopes')
    vat_description=models.CharField('НДС по договору — условия и ставка',max_length=200,blank=True)
    planned_profit=models.DecimalField('Плановая прибыль, ₽',max_digits=18,decimal_places=2,null=True,blank=True)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    def __str__(self):return self.title

class FinancialImport(models.Model):
    source_version=models.ForeignKey('documents.DocumentVersion',on_delete=models.PROTECT,null=True,blank=True)
    source_document=models.ForeignKey('documents.Document',on_delete=models.PROTECT,null=True,blank=True)
    sha256=models.CharField(max_length=64,unique=True)
    original_name=models.CharField(max_length=200)
    uploaded_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at=models.DateTimeField(auto_now_add=True)

class OpeningSnapshot(ArchiveProtectedModel):
    scope=models.OneToOneField(FinancialScope,on_delete=models.PROTECT,related_name='opening')
    source=models.ForeignKey(FinancialImport,on_delete=models.PROTECT)
    total_defects=models.PositiveIntegerField()
    repaired_defects=models.PositiveIntegerField()
    advance_received=models.DecimalField(max_digits=18,decimal_places=2)
    accepted=models.DecimalField(max_digits=18,decimal_places=2)
    paid_for_work=models.DecimalField(max_digits=18,decimal_places=2)
    advance_offset=models.DecimalField(max_digits=18,decimal_places=2)
    declared_advance_balance=models.DecimalField(max_digits=18,decimal_places=2)
    declared_pending=models.DecimalField(max_digits=18,decimal_places=2)

class PendingWork(ArchiveProtectedModel):
    scope=models.ForeignKey(FinancialScope,on_delete=models.PROTECT,related_name='pending_work')
    title=models.CharField('Выполнение / корректировка',max_length=250)
    amount=models.DecimalField('Сумма, ₽',max_digits=18,decimal_places=2)
    stage=models.CharField('Состояние по источнику',max_length=250,blank=True)
    source=models.ForeignKey(FinancialImport,on_delete=models.PROTECT,null=True,blank=True)

class CashReceipt(models.Model):
    legal_entity=models.ForeignKey('projects.LegalEntity',on_delete=models.PROTECT,null=True)
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    token=models.UUIDField(unique=True)
    date=models.DateField('Дата поступления')
    amount=models.DecimalField('Получено, ₽',max_digits=18,decimal_places=2)
    source=models.CharField('Источник / счёт',max_length=200)
    reference=models.CharField('Номер платежа / основание',max_length=200)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at=models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints=[models.CheckConstraint(condition=models.Q(amount__gt=0),name='amount_positive')]

class PaymentAllocation(ArchiveProtectedModel):
    reversed=models.BooleanField(default=False)
    reversal_reason=models.CharField(max_length=500,blank=True)
    receipt=models.ForeignKey(CashReceipt,on_delete=models.PROTECT,related_name='allocations')
    scope=models.ForeignKey(FinancialScope,on_delete=models.PROTECT,related_name='allocations')
    purpose=models.CharField('Назначение',max_length=20,choices=[('advance','Аванс'),('work','Оплата выполнения')])
    amount=models.DecimalField('Отнести сумму, ₽',max_digits=18,decimal_places=2)
    work=models.ForeignKey('WorkExecution',on_delete=models.PROTECT,null=True,blank=True,related_name='payments')
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at=models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints=[models.CheckConstraint(condition=models.Q(amount__gt=0),name='allocation_positive')]

class WorkExecution(ArchiveProtectedModel):
    source_pending=models.OneToOneField(PendingWork,on_delete=models.PROTECT,null=True,blank=True,related_name='execution')
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    scope=models.ForeignKey(FinancialScope,on_delete=models.PROTECT,related_name='executions')
    title=models.CharField('Выполнение / КС',max_length=200)
    date=models.DateField('Дата выполнения')
    factual_amount=models.DecimalField('Выполнено фактически, ₽',max_digits=18,decimal_places=2)
    accepted_amount=models.DecimalField('Принято по КС, ₽',max_digits=18,decimal_places=2,default=0)
    payment_due=models.DateField('Срок оплаты по договору',null=True,blank=True)
    basis=models.CharField('Основание принятия / срока оплаты',max_length=300,blank=True)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    accepted_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name='+')
    accepted_at=models.DateTimeField(null=True,blank=True)
    revision=models.PositiveIntegerField(default=1)
    def __str__(self):return self.title

    class Meta:
        constraints=[models.CheckConstraint(condition=models.Q(factual_amount__gt=0,accepted_amount__gte=0,accepted_amount__lte=models.F('factual_amount')),name='work_amounts_valid')]

class AdvanceOffset(ArchiveProtectedModel):
    reversed=models.BooleanField(default=False)
    reversal_reason=models.CharField(max_length=500,blank=True)
    work=models.ForeignKey(WorkExecution,on_delete=models.PROTECT,related_name='offsets')
    amount=models.DecimalField('Зачтено аванса, ₽',max_digits=18,decimal_places=2)
    date=models.DateField('Дата зачёта')
    basis=models.CharField('Основание / условие договора',max_length=300)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)

    class Meta:
        constraints=[models.CheckConstraint(condition=models.Q(amount__gt=0),name='offset_positive')]

class CostEntry(ArchiveProtectedModel):
    project_object=models.ForeignKey('projects.ProjectObject',on_delete=models.PROTECT,null=True,blank=True,related_name='financial_costs')
    legal_entity=models.ForeignKey('projects.LegalEntity',on_delete=models.PROTECT,null=True)
    scope=models.ForeignKey(FinancialScope,on_delete=models.PROTECT,null=True,blank=True,related_name='costs')
    date=models.DateField('Дата')
    kind=models.CharField('Вид затрат',max_length=20,choices=[('direct','Прямые'),('overhead','Накладные')])
    amount=models.DecimalField('Затраты, ₽',max_digits=18,decimal_places=2)
    paid_amount=models.DecimalField('Фактически оплачено, ₽',max_digits=18,decimal_places=2,default=0)
    basis=models.CharField('Статья / основание',max_length=300)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)

    class Meta:
        constraints=[models.CheckConstraint(condition=models.Q(amount__gt=0,paid_amount__gte=0,paid_amount__lte=models.F('amount')),name='cost_valid')]

class SubcontractObligation(ArchiveProtectedModel):
    scope=models.ForeignKey(FinancialScope,on_delete=models.PROTECT,related_name='subcontracts')
    counterparty=models.CharField('Субподрядчик',max_length=200)
    basis=models.CharField('Договор / основание',max_length=300)
    amount=models.DecimalField('Обязательство, ₽',max_digits=18,decimal_places=2)
    paid=models.DecimalField('Оплачено, ₽',max_digits=18,decimal_places=2,default=0)
    due=models.DateField('Срок оплаты',null=True,blank=True)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)

    class Meta:
        constraints=[models.CheckConstraint(condition=models.Q(amount__gt=0,paid__gte=0,paid__lte=models.F('amount')),name='subcontract_valid')]

class PriceChange(ArchiveProtectedModel):
    contract=models.ForeignKey('projects.Contract',on_delete=models.PROTECT,related_name='price_changes')
    date=models.DateField('Дата изменения')
    amount=models.DecimalField('Изменение цены, ₽ (может быть отрицательным)',max_digits=18,decimal_places=2)
    basis=models.CharField('Дополнительное соглашение / основание',max_length=300)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)

class FinancialDraft(models.Model):
    source_version=models.ForeignKey('documents.DocumentVersion',on_delete=models.PROTECT,null=True,blank=True)
    source_document=models.ForeignKey('documents.Document',on_delete=models.PROTECT,null=True,blank=True)
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE)
    original_name=models.CharField(max_length=200)
    payload=models.JSONField(encoder=DjangoJSONEncoder)
    created_at=models.DateTimeField(auto_now_add=True)

class FinancialOperation(models.Model):
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    token=models.UUIDField()
    result_path=models.CharField(max_length=250)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['user','token'],name='unique_financial_operation')]
