"""Additional ledgers; existing business records are retained by migration."""
import uuid
from django.conf import settings
from django.db import models


class EquipmentRental(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    placement=models.OneToOneField('projects.EquipmentAssignment',on_delete=models.PROTECT,related_name='rental')
    agreement=models.ForeignKey('projects.CounterpartyAgreement',null=True,blank=True,on_delete=models.PROTECT)
    supplier=models.CharField('Арендодатель',max_length=200)
    reference=models.CharField('Договор аренды',max_length=200)
    financial_scope=models.ForeignKey('finance.FinancialScope',null=True,blank=True,on_delete=models.PROTECT,related_name='equipment_rentals')
    version=models.PositiveIntegerField(default=1)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at=models.DateTimeField(auto_now_add=True)


class RentalRate(models.Model):
    rental=models.ForeignKey(EquipmentRental,on_delete=models.PROTECT,related_name='rates')
    starts_on=models.DateField('Действует с')
    basis=models.CharField('Расчёт',max_length=12,choices=[('day','За день / смену'),('hour','За час')])
    work_price=models.DecimalField('Работа: ставка, ₽',max_digits=16,decimal_places=2)
    idle_price=models.DecimalField('Наш простой: ставка, ₽',max_digits=16,decimal_places=2)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    class Meta:
        ordering=['-starts_on']
        constraints=[models.UniqueConstraint(fields=['rental','starts_on'],name='rental_rate_date_unique'),models.CheckConstraint(condition=models.Q(work_price__gte=0,idle_price__gte=0),name='rental_rate_nonnegative')]


class RentalPeriod(models.Model):
    rental=models.ForeignKey(EquipmentRental,on_delete=models.PROTECT,related_name='periods')
    starts_on=models.DateField('С')
    ends_on=models.DateField('По включительно')
    snapshot=models.JSONField(default=dict)
    reopened_at=models.DateTimeField(null=True,blank=True)
    reopened_by=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,blank=True,on_delete=models.PROTECT,related_name='+')
    reopen_reason=models.CharField(max_length=500,blank=True)
    note=models.CharField(max_length=500,blank=True)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=['-ends_on']
        constraints=[models.CheckConstraint(condition=models.Q(ends_on__gte=models.F('starts_on')),name='rental_period_dates')]


class RentalDay(models.Model):
    rental=models.ForeignKey(EquipmentRental,on_delete=models.PROTECT,related_name='days')
    date=models.DateField('Дата')
    kind=models.CharField(max_length=12,choices=[('work','Работал — 10 ч'),('idle','Наш простой — 4 ч'),('external','Не по нашей вине — 0 ч')])
    hours=models.PositiveSmallIntegerField(default=0)
    amount=models.DecimalField(max_digits=18,decimal_places=2,default=0)
    rate=models.ForeignKey(RentalRate,null=True,on_delete=models.PROTECT)
    version=models.PositiveIntegerField(default=1)
    updated_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    updated_at=models.DateTimeField(auto_now=True)
    class Meta:
        ordering=['-date']
        constraints=[models.UniqueConstraint(fields=['rental','date'],name='rental_day_unique'),models.CheckConstraint(condition=models.Q(amount__gte=0,hours__in=[0,4,10]),name='rental_day_values')]


class RentalDayRevision(models.Model):
    day=models.ForeignKey(RentalDay,on_delete=models.PROTECT,related_name='revisions')
    number=models.PositiveIntegerField()
    data=models.JSONField()
    author=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['day','number'],name='rental_day_revision_unique')]


class RentalPayment(models.Model):
    rental=models.ForeignKey(EquipmentRental,on_delete=models.PROTECT,related_name='payments')
    token=models.UUIDField(unique=True,default=uuid.uuid4)
    date=models.DateField('Дата перечисления')
    amount=models.DecimalField('Перечислено, ₽',max_digits=18,decimal_places=2)
    note=models.CharField('Основание / аванс / платёж',max_length=500)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at=models.DateTimeField(auto_now_add=True)
    reversed=models.BooleanField(default=False)
    reversal_reason=models.CharField(max_length=500,blank=True)
    class Meta:
        ordering=['-date','-pk']
        constraints=[models.CheckConstraint(condition=models.Q(amount__gt=0),name='rental_payment_positive')]


class CoatingProfile(models.Model):
    method=models.CharField('Конструкция',max_length=10,choices=[('p1','П1'),('p2','П2')],default='p2')
    diameter_mm=models.PositiveIntegerField('Наружный диаметр трубы, мм')
    length_mm=models.PositiveIntegerField('Длина муфты по карточке, мм')
    full_length_mm=models.PositiveIntegerField('Полный габарит с кольцами, мм')
    area_per_m=models.DecimalField('Площадь на метр изолируемой длины, м²/м',max_digits=10,decimal_places=6)
    extra_length_mm=models.PositiveIntegerField('Дополнительная длина покрытия, мм',default=1000)
    tape=models.ForeignKey('projects.StockItem',on_delete=models.PROTECT,related_name='+',verbose_name='Ксизол (кг)')
    wrapper=models.ForeignKey('projects.StockItem',on_delete=models.PROTECT,related_name='+',verbose_name='Обёртка Полилен (кг)')
    primer=models.ForeignKey('projects.StockItem',on_delete=models.PROTECT,related_name='+',verbose_name='Праймер (кг)')
    basis=models.CharField('Основание площади и габарита',max_length=500)
    active=models.BooleanField(default=True)
    version=models.PositiveIntegerField(default=1)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    class Meta:
        ordering=['diameter_mm','length_mm']
        constraints=[models.UniqueConstraint(fields=['method','diameter_mm','length_mm'],name='coating_profile_size_unique'),models.CheckConstraint(condition=models.Q(area_per_m__gt=0,full_length_mm__gte=models.F('length_mm')),name='coating_profile_positive')]


class RepairConsumption(models.Model):
    job=models.OneToOneField('repairs.Job',on_delete=models.PROTECT,related_name='material_consumption')
    report=models.ForeignKey('repairs.Report',on_delete=models.PROTECT)
    fingerprint=models.CharField(max_length=64)
    state=models.CharField(max_length=12,choices=[('posted','Списано по норме'),('pending','Требует списания'),('review','Требует проверки исправления')])
    review_reason=models.CharField('Причина проверки',max_length=24,blank=True,default='',choices=[('manual','Ручное списание'),('legacy','Прежний результат П1'),('installation','Изменена конструкция')])
    lines=models.JSONField(default=list)
    message=models.CharField(max_length=2000,blank=True)
    updated_at=models.DateTimeField(auto_now=True)


class CustomerDocumentLink(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    contract=models.ForeignKey('projects.Contract',on_delete=models.PROTECT)
    project_object=models.ForeignKey('projects.ProjectObject',on_delete=models.PROTECT)
    document=models.ForeignKey('documents.Document',on_delete=models.PROTECT)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at=models.DateTimeField(auto_now_add=True)
    active=models.BooleanField(default=True)
    class Meta:
        ordering=['-created_at']
        constraints=[models.UniqueConstraint(fields=['contract','project_object','document'],name='customer_document_scope_unique')]
