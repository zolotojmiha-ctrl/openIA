import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone

class Mailbox(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    address=models.EmailField(unique=True)
    owner=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name='mailboxes')
    person=models.ForeignKey('documents.Person',on_delete=models.PROTECT,null=True,blank=True,related_name='mailboxes')
    state=models.CharField(max_length=16,choices=[('requested','Ожидает создания у провайдера'),('active','Подключён'),('disabled','Доступ через ЛК отключён')],default='requested')
    credential=models.TextField(blank=True)
    version=models.PositiveIntegerField(default=1)
    unread=models.PositiveIntegerField(default=0)
    unread_digest=models.CharField(max_length=64,blank=True)
    synced_at=models.DateTimeField(null=True,blank=True)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    def __str__(self):return self.address

class Letter(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    mailbox=models.ForeignKey(Mailbox,on_delete=models.PROTECT,related_name='letters')
    author=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    to=models.CharField(max_length=1500,blank=True)
    cc=models.CharField(max_length=1500,blank=True)
    bcc=models.CharField(max_length=1500,blank=True)
    subject=models.CharField(max_length=250,blank=True)
    body=models.TextField(max_length=100000,blank=True)
    state=models.CharField(max_length=16,choices=[('draft','Черновик'),('queued','В очереди'),('sending','Отправляется'),('sent','Отправлено'),('failed','Не отправлено'),('uncertain','Результат отправки требует проверки')],default='draft')
    version=models.PositiveIntegerField(default=1)
    in_reply_to=models.CharField(max_length=200,blank=True)
    message_id=models.CharField(max_length=200,blank=True)
    error=models.CharField(max_length=250,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    updated_at=models.DateTimeField(auto_now=True)
    sent_at=models.DateTimeField(null=True,blank=True)

class Attachment(models.Model):
    letter=models.ForeignKey(Letter,on_delete=models.CASCADE,related_name='attachments')
    name=models.CharField(max_length=200)
    data=models.BinaryField()


class SiteRequest(models.Model):
    """A durable receipt, independent of availability of the SMTP provider."""
    KINDS=[('candidate','Анкета кандидата'),('rental','Аренда техники'),('couplings','Муфты П1 и П2'),('centralizers','Звеньевые центраторы')]
    DELIVERY=[('queued','Ожидает отправки'),('sending','Отправляется'),('sent','Передано на почту'),('failed','Почта не приняла письмо'),('uncertain','Проверьте получение письма')]
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    kind=models.CharField(max_length=20,choices=KINDS)
    data=models.JSONField(default=dict)
    digest=models.CharField(max_length=64)
    consent_version=models.CharField(max_length=40)
    created_at=models.DateTimeField(auto_now_add=True,db_index=True)
    updated_at=models.DateTimeField(auto_now=True)
    resume_name=models.CharField(max_length=200,blank=True)
    resume_type=models.CharField(max_length=100,blank=True)
    resume_data=models.BinaryField(default=bytes,blank=True)
    delivery_state=models.CharField(max_length=16,choices=DELIVERY,default='queued')
    delivery_error=models.CharField(max_length=300,blank=True)
    delivery_attempts=models.PositiveIntegerField(default=0)
    next_attempt_at=models.DateTimeField(default=timezone.now)
    sent_at=models.DateTimeField(null=True,blank=True)
    read_by=models.ManyToManyField(settings.AUTH_USER_MODEL,blank=True,related_name='read_site_requests')

    class Meta:
        ordering=['-created_at','-pk']


class SiteRequestAttempt(models.Model):
    key=models.CharField(max_length=64,primary_key=True)
    count=models.PositiveIntegerField(default=0)
    window_start=models.DateTimeField(default=timezone.now)

class MailboxImportReceipt(models.Model):
    token=models.UUIDField(unique=True)
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at=models.DateTimeField(auto_now_add=True)

class BegetSettings(models.Model):
    id=models.PositiveSmallIntegerField(primary_key=True,default=1,editable=False)
    login=models.CharField(max_length=100,blank=True)
    credential=models.TextField(blank=True)
    enabled=models.BooleanField(default=False)
    version=models.PositiveIntegerField(default=1)

class MailboxProvision(models.Model):
    mailbox=models.OneToOneField(Mailbox,on_delete=models.PROTECT,related_name='provision')
    credential=models.TextField()
    mailbox_version=models.PositiveIntegerField()
    state=models.CharField(max_length=20,choices=[('queued','Ожидает создания'),('creating','Создаётся в Beget'),('active','Создан и подключён'),('failed','Не создан — проверьте настройку'),('uncertain','Проверьте результат в Beget')],default='queued')
    error=models.CharField(max_length=300,blank=True)
    updated_at=models.DateTimeField(auto_now=True)
