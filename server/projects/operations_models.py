"""Dated operational records and explicitly published customer snapshots."""
import uuid
from django.conf import settings
from django.db import models

class EquipmentCustody(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    equipment=models.ForeignKey('projects.Equipment',on_delete=models.PROTECT,related_name='custodies')
    brigade=models.ForeignKey('projects.Brigade',on_delete=models.PROTECT,null=True,blank=True,related_name='equipment_custodies',verbose_name='Бригада')
    driver=models.ForeignKey('documents.Person',on_delete=models.PROTECT,null=True,blank=True,related_name='equipment_custodies',verbose_name='Водитель / машинист')
    start_date=models.DateField('Начало закрепления')
    end_date=models.DateField('Окончание',null=True,blank=True)
    version=models.PositiveIntegerField(default=1)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    class Meta:
        constraints=[models.CheckConstraint(condition=models.Q(brigade__isnull=False)|models.Q(driver__isnull=False),name='custody_target'),models.CheckConstraint(condition=models.Q(end_date__isnull=True)|models.Q(end_date__gte=models.F('start_date')),name='custody_dates')]

class Admission(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    brigade=models.ForeignKey('projects.Brigade',on_delete=models.PROTECT,related_name='admissions')
    contract=models.ForeignKey('projects.Contract',on_delete=models.PROTECT)
    project_object=models.ForeignKey('projects.ProjectObject',on_delete=models.PROTECT)
    title=models.CharField('Название комплекта / виды работ',max_length=250)
    version=models.PositiveIntegerField(default=0)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)

class AdmissionRevision(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    admission=models.ForeignKey(Admission,on_delete=models.PROTECT,related_name='revisions')
    number=models.PositiveIntegerField()
    status=models.CharField('Состояние',max_length=20,choices=[('draft','Готовится'),('submitted','Направлен на согласование'),('approved','Допуск согласован'),('instructed','Инструктаж пройден'),('closed','Завершён')])
    start_date=models.DateField('Допуск с')
    end_date=models.DateField('Допуск по',null=True,blank=True)
    instruction_date=models.DateField('Дата инструктажа',null=True,blank=True)
    instruction_place=models.CharField('Место инструктажа / НПС',max_length=200,blank=True)
    note=models.TextField('Основание согласования / примечание',max_length=3000,blank=True)
    snapshot=models.JSONField(default=dict)
    documents=models.ManyToManyField('documents.DocumentVersion',through='AdmissionFile')
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=['-number']
        constraints=[models.UniqueConstraint(fields=['admission','number'],name='admission_revision_number')]

class AdmissionFile(models.Model):
    revision=models.ForeignKey(AdmissionRevision,on_delete=models.PROTECT,related_name='files')
    version=models.ForeignKey('documents.DocumentVersion',on_delete=models.PROTECT)
    title=models.CharField(max_length=200)
    category=models.CharField(max_length=80)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['revision','version'],name='admission_file_unique')]

class CustomerAccess(models.Model):
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    contract=models.ForeignKey('projects.Contract',on_delete=models.PROTECT)
    project_object=models.ForeignKey('projects.ProjectObject',on_delete=models.PROTECT)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['user','contract','project_object'],name='customer_area_access')]

class CustomerPublication(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    contract=models.ForeignKey('projects.Contract',on_delete=models.PROTECT)
    project_object=models.ForeignKey('projects.ProjectObject',on_delete=models.PROTECT)
    snapshot=models.JSONField(default=dict)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:ordering=['-created_at','-pk']

class MaterialRequest(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    contract=models.ForeignKey('projects.Contract',on_delete=models.PROTECT)
    project_object=models.ForeignKey('projects.ProjectObject',on_delete=models.PROTECT)
    name=models.CharField('Материал / тип муфты',max_length=200)
    quantity=models.DecimalField('Количество',max_digits=12,decimal_places=3)
    unit=models.CharField('Единица измерения',max_length=30)
    kilometre=models.CharField('Километр / диапазон',max_length=100,blank=True)
    needed_on=models.DateField('Требуется к',null=True,blank=True)
    note=models.CharField('Пояснение для заказчика',max_length=1000,blank=True)
    status=models.CharField('Состояние',max_length=20,choices=[('needed','Требуется'),('ordered','Заказано'),('provided','Обеспечено'),('cancelled','Отменено')],default='needed')
    version=models.PositiveIntegerField(default=1)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    class Meta:
        constraints=[models.CheckConstraint(condition=models.Q(quantity__gt=0),name='material_request_quantity')]
