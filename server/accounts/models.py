import re
import uuid
from datetime import timedelta
from django.utils import timezone
from django.conf import settings
from django.core.exceptions import ValidationError
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.utils.crypto import salted_hmac

PERMISSIONS = {
 'document.read':'Просматривать документы', 'document.upload':'Загружать документы',
 'document.replace':'Заменять файл новой версией', 'document.archive':'Архивировать документы',
 'document.restore':'Восстанавливать из архива', 'document.download':'Скачивать документы',
 'document.export':'Экспортировать документы', 'document.personal':'Доступ к персональным документам',
 'document.subcontractor':'Доступ к документам субподрядчиков',
}
ROLES = [('accounting','Бухгалтерия'),('foreman','Мастер / прораб'),('mechanic','Механик'),('safety','Охрана труда и безопасность'),('ecology','Эколог'),('supply','Снабжение / склад'),('as_built','Исполнительная документация'),('subcontractor','Субподрядчик')]

def normalize_phone(value):
    value = str(value).strip()
    if re.search(r'[^\d+()\s-]', value):
        raise ValidationError('Укажите телефон в международном формате, например +7 …')
    digits = re.sub(r'[^0-9]', '', value)
    if len(digits)==11 and digits.startswith('8'):
        digits='7'+digits[1:]
    if not re.fullmatch(r'[1-9][0-9]{7,14}', digits):
        raise ValidationError('Укажите телефон в международном формате: от 8 до 15 цифр.')
    return '+'+digits

def temporary_deadline():
    return timezone.now()+timedelta(hours=24)

class UserManager(BaseUserManager):
    def create_user(self, phone, password=None, **extra):
        user=self.model(phone=normalize_phone(phone),**extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

class User(AbstractBaseUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone = models.CharField(max_length=16, unique=True)
    full_name = models.CharField(max_length=150)
    leader_slot = models.PositiveSmallIntegerField(null=True, blank=True, unique=True)
    role = models.CharField(max_length=20, choices=ROLES, blank=True)
    is_active = models.BooleanField(default=True)
    must_change_password = models.BooleanField(default=True)
    temporary_expires_at = models.DateTimeField(default=temporary_deadline,null=True,blank=True)
    session_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = UserManager()
    USERNAME_FIELD='phone'
    class Meta:
        constraints=[models.CheckConstraint(condition=models.Q(leader_slot__isnull=True)|models.Q(leader_slot__in=[1,2]),name='only_two_leader_slots')]
    @property
    def is_leader(self):
        return self.leader_slot in (1,2)
    def get_session_auth_hash(self):
        return salted_hmac('armada.session',f'{self.password}:{self.session_version}',secret=settings.SECRET_KEY,algorithm='sha256').hexdigest()

class AccessGrant(models.Model):
    user=models.ForeignKey(User,on_delete=models.CASCADE,related_name='grants')
    permission=models.CharField(max_length=40,choices=[(k,v) for k,v in PERMISSIONS.items()])
    granted_by=models.ForeignKey(User,on_delete=models.PROTECT,related_name='+')
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['user','permission'],name='unique_user_permission')]

class AuditEvent(models.Model):
    ACTIONS=[('leader_bootstrap','Созданы руководители'),('login','Вход'),('logout','Выход'),('password_changed','Изменён пароль'),('employee_created','Создан сотрудник'),('employee_updated','Изменены данные и права'),('employee_password_reset','Сброшен пароль'),('object_created','Создан объект'),('object_updated','Изменён объект'),('object_access_changed','Назначен доступ к объекту'),('contract_created','Создан договор'),('contract_updated','Изменён договор'),('contract_access_changed','Изменён доступ к договору')]
    actor=models.ForeignKey(User,null=True,on_delete=models.PROTECT,related_name='+')
    subject=models.ForeignKey(User,null=True,on_delete=models.PROTECT,related_name='+')
    action=models.CharField(max_length=60,choices=ACTIONS)
    details=models.JSONField(default=dict)
    created_at=models.DateTimeField(auto_now_add=True)

class LoginAttempt(models.Model):
    key=models.CharField(max_length=64,unique=True)
    count=models.PositiveIntegerField(default=0)
    window_start=models.DateTimeField()
