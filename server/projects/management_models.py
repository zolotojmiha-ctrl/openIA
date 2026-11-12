import uuid
from django.conf import settings
from django.db import models


class EquipmentIssue(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    token=models.UUIDField(unique=True)
    equipment=models.ForeignKey('projects.Equipment',on_delete=models.PROTECT,related_name='issues')
    placement=models.ForeignKey('projects.EquipmentAssignment',on_delete=models.PROTECT)
    author=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    description=models.TextField(max_length=3000)
    state=models.CharField(max_length=12,choices=[('open','Открыта'),('closed','Рассмотрена')],default='open')
    created_at=models.DateTimeField(auto_now_add=True)
    resolved_at=models.DateTimeField(null=True,blank=True)
    resolved_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name='+')
    resolution=models.TextField(max_length=3000,blank=True)


class PhotoArchive(models.Model):
    storage_key=models.CharField(max_length=40,primary_key=True)
    sha256=models.CharField(max_length=64)
    size=models.PositiveIntegerField()
    remote_path=models.CharField(max_length=200)
    verified_at=models.DateTimeField()
    local_removed_at=models.DateTimeField(null=True,blank=True)
