import uuid
from datetime import time
from django.conf import settings
from django.db import models
from django.utils import timezone

class DiskBackupSettings(models.Model):
    id=models.PositiveSmallIntegerField(primary_key=True,default=1,editable=False)
    credential=models.TextField(blank=True)
    version=models.PositiveIntegerField(default=1)
    checked_version=models.PositiveIntegerField(default=0)
    checked_at=models.DateTimeField(null=True,blank=True)
    total_space=models.BigIntegerField(default=0)
    used_space=models.BigIntegerField(default=0)
    public_key=models.TextField(blank=True)
    key_fingerprint=models.CharField(max_length=64,blank=True)
    key_confirmed=models.BooleanField(default=False)
    enabled=models.BooleanField(default=False)
    photos_enabled=models.BooleanField(default=False)
    photos_remove_local=models.BooleanField(default=False)
    backup_time=models.TimeField(default=time(3))
    not_before=models.DateTimeField(default=timezone.now)
    worker_seen_at=models.DateTimeField(null=True,blank=True)
    updated_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True)
    error=models.CharField(max_length=300,blank=True)

class DiskBackupJob(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    config_version=models.PositiveIntegerField()
    key_fingerprint=models.CharField(max_length=64)
    public_key=models.TextField()
    state=models.CharField(max_length=16,default='queued',choices=[('queued','Ожидает создания'),('running','Создаётся и проверяется'),('verified','Копия загружена и проверена'),('failed','Не завершено')])
    automatic_day=models.DateField(null=True,blank=True,unique=True)
    author=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    started_at=models.DateTimeField(null=True,blank=True)
    verified_at=models.DateTimeField(null=True,blank=True)
    remote_path=models.CharField(max_length=200,blank=True)
    sha256=models.CharField(max_length=64,blank=True)
    size=models.BigIntegerField(default=0)
    error=models.CharField(max_length=300,blank=True)
    class Meta:ordering=['-created_at']
