import uuid
from datetime import time
from django.conf import settings
from django.db import models
from django.utils import timezone as dj_timezone

class DemandMailing(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    name=models.CharField('Название рассылки',max_length=150)
    owner=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    mailbox=models.ForeignKey('postoffice.Mailbox',on_delete=models.PROTECT)
    recipients=models.TextField('Получатели')
    project_objects=models.ManyToManyField('projects.ProjectObject')
    sections=models.ManyToManyField('repairs.Section',blank=True)
    contracts=models.ManyToManyField('projects.Contract',blank=True)
    enabled=models.BooleanField('Еженедельная рассылка включена',default=False)
    weekday=models.PositiveSmallIntegerField(default=0)
    send_time=models.TimeField(default=time(9))
    timezone=models.CharField(max_length=40,default='Asia/Omsk')
    not_before=models.DateTimeField(default=dj_timezone.now)
    version=models.PositiveIntegerField(default=1)
    last_error=models.CharField(max_length=300,blank=True)
    updated_at=models.DateTimeField(auto_now=True)

class DemandMailRun(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    mailing=models.ForeignKey(DemandMailing,on_delete=models.PROTECT,related_name='runs')
    author=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    version=models.PositiveIntegerField()
    automatic=models.BooleanField(default=False)
    week=models.DateField(null=True,blank=True)
    snapshot=models.JSONField()
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['mailing','week'],condition=models.Q(automatic=True),name='demand_mail_week_once')]
        ordering=['-created_at']

class DemandMailDelivery(models.Model):
    run=models.ForeignKey(DemandMailRun,on_delete=models.PROTECT,related_name='deliveries')
    letter=models.OneToOneField('postoffice.Letter',on_delete=models.PROTECT,related_name='demand_delivery')
