"""Durable MAX links, conversations and delivery queues. No credentials in rows."""
import uuid
from django.conf import settings
from django.db import models


class Binding(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    max_user = models.BigIntegerField()
    chat = models.BigIntegerField()
    session_version = models.PositiveIntegerField()
    last_timestamp = models.BigIntegerField(default=0)
    menu_scope = models.JSONField(default=dict)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user'], condition=models.Q(active=True), name='max_active_employee'),
            models.UniqueConstraint(fields=['max_user'], condition=models.Q(active=True), name='max_active_sender'),
        ]


class LinkTicket(models.Model):
    digest = models.CharField(primary_key=True, max_length=64)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    session_version = models.PositiveIntegerField()
    expires = models.DateTimeField()
    max_user = models.BigIntegerField(null=True)
    chat = models.BigIntegerField(null=True)
    consumed = models.BooleanField(default=False)


class Draft(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    binding = models.ForeignKey(Binding, on_delete=models.PROTECT)
    job = models.ForeignKey('repairs.Job', on_delete=models.PROTECT)
    action = models.CharField(max_length=16)
    raw = models.JSONField(default=dict)
    fields = models.JSONField(default=list)
    step = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=16, default='editing')
    expires = models.DateTimeField()
    report = models.OneToOneField('repairs.Report', null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class Photo(models.Model):
    draft = models.ForeignKey(Draft, on_delete=models.CASCADE, related_name='photos')
    storage_key = models.CharField(max_length=100)
    sha256 = models.CharField(max_length=64)
    size = models.PositiveIntegerField()
    name = models.CharField(max_length=200)


class Inbox(models.Model):
    key = models.CharField(primary_key=True, max_length=64)
    max_user = models.BigIntegerField()
    chat = models.BigIntegerField()
    payload = models.JSONField()
    state = models.CharField(max_length=16, default='pending', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True)


class Outbox(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=150, unique=True)
    binding = models.ForeignKey(Binding, null=True, on_delete=models.PROTECT)
    job = models.ForeignKey('repairs.Job', null=True, on_delete=models.PROTECT)
    max_user = models.BigIntegerField()
    chat = models.BigIntegerField()
    body = models.JSONField()
    scope = models.JSONField(default=dict)
    state = models.CharField(max_length=16, default='pending', db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField()
    expires = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)


class RateBucket(models.Model):
    key = models.CharField(primary_key=True, max_length=100)
    count = models.PositiveIntegerField(default=0)


class DemandWatch(models.Model):
    format_version=models.PositiveSmallIntegerField(default=2)
    binding=models.OneToOneField(Binding,on_delete=models.CASCADE)
    digest=models.CharField(max_length=64,blank=True)
    version=models.PositiveIntegerField(default=0)
