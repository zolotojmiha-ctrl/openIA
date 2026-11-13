import uuid
from django.conf import settings
from django.db import models


class RentalAccess(models.Model):
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='rental_accesses')
    rental=models.ForeignKey('projects.EquipmentRental',on_delete=models.PROTECT,related_name='external_accesses')
    active=models.BooleanField(default=True)
    financial=models.BooleanField('Показывать тарифы, начисления и оплаты этой аренды',default=False)
    version=models.PositiveIntegerField(default=1)
    granted_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['user','rental'],name='rental_external_access_unique')]


class RentalDispute(models.Model):
    access=models.ForeignKey(RentalAccess,on_delete=models.PROTECT,related_name='disputes')
    date=models.DateField('Дата в табеле')
    text=models.CharField('Разногласие',max_length=2000)
    token=models.UUIDField(default=uuid.uuid4,unique=True)
    created_at=models.DateTimeField(auto_now_add=True)
    resolved_at=models.DateTimeField(null=True,blank=True)
    resolved_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name='+')
    resolution=models.CharField(max_length=2000,blank=True)
