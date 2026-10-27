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
 'works.prepare':'Готовить и корректировать ежедневные ведомости работ',
 'contract.changes':'Вести указания заказчика и изменения контрактов',
 'subcontract.manage':'Вести субподрядчиков, поручения и доступ представителей',
 'stock.manage':'Вести склады, остатки и движение материалов',
 'equipment.manage':'Вести технику, документы и назначения на объекты',
 'users.manage':'Управлять учётными записями сотрудников',
 'projects.manage':'Создавать и изменять объекты и контракты',
 'projects.access':'Настраивать доступ к назначенным объектам и контрактам',
 'repairs.prepare':'Вести ведомости дефектов и назначать работы мастерам',
 'document.upload':'Загружать документы',
 'document.export':'Экспортировать список документов в CSV',
 'personnel.manage':'Вести карточки персонала и назначения',
 'finance.read':'Просматривать финансовый учёт',
 'finance.write':'Вносить финансовые записи и распределять выплаты',
}

ROLES = [('technician','Техник'),('pto','Инженер ПТО'),('other','Другая должность'),('accounting','Бухгалтерия'),('foreman','Мастер / прораб'),('mechanic','Механик'),('safety','Инженер по ОТ и ПБ'),('ecology','Эколог'),('supply','Снабжение / склад'),('as_built','Исполнительная документация'),('subcontractor','Субподрядчик'),('customer','Заказчик')]

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
    ACTIONS += [(key,label) for key,label in [('document_uploaded','Загружен документ'),('document_replaced','Новая версия документа'),('document_decided','Решение по версии'),('document_archived','Документ в архиве'),('document_restored','Документ восстановлен'),('document_access_changed','Изменён доступ к документу'),('document_downloaded','Скачан документ'),('document_exported','Экспорт списка документов'),('document_integrity_error','Ошибка сохранности файла')]]
    ACTIONS += [('document_links_changed','Изменены связи документа'),('document_commented','Комментарий к документу'),('document_bulk_changed','Массовое изменение документов'),('person_updated','Изменена карточка работника'),('person_assigned','Назначение работника'),('person_archived','Архив работника'),('sessions_ended','Завершены сеансы'),('support_updated','Обращение сотрудника'),('finance_created','Финансовая запись'),('finance_allocated','Распределена выплата'),('finance_accepted','Подтверждено принятое выполнение'),('finance_imported','Загружена финансовая справка')]
    ACTIONS += [('document_responsibility_changed','Передана ответственность за документ'),('document_bundle_primary','Выбран основной файл комплекта'),('finance_corrected','Исправлена финансовая запись'),('finance_offset_reversed','Отменён зачёт аванса'),('finance_allocation_reversed','Отменено распределение выплаты'),('finance_scope_updated','Изменены параметры финансового направления')]
    ACTIONS += [('repair_prepared','Подготовлены ремонтные работы'),('repair_reported','Внесены сведения с участка'),('repair_confirmed','Подтверждено выполнение ремонта'),('repair_corrected','Исправлен результат ремонта'),('repair_accepted','Отмечена приёмка ремонта'),('repair_imported','Перенесена ведомость дефектов'),('repair_exported','Экспортирована ведомость дефектов'),('repair_defect_archived','Дефект удалён из списка'),('repair_defect_restored','Дефект восстановлен')]
    ACTIONS += [('equipment_updated','Изменена техника'),('equipment_assigned','Назначение техники'),('equipment_document_updated','Документ техники'),('equipment_imported','Импорт техники')]
    ACTIONS += [('stock_updated','Изменены складские справочники'),('stock_access_changed','Доступ к складу'),('stock_moved','Складская операция'),('stock_imported','Импорт складских данных')]
    ACTIONS += [('subcontract_updated','Изменён субподряд'),('subcontract_access_changed','Доступ к субподряду'),('subcontract_reported','Результат субподрядчика'),('subcontract_decided','Проверка субподряда'),('subcontract_document_linked','Документ субподрядчика'),('subcontract_imported','Импорт субподряда')]
    ACTIONS += [('document_assembled','Собран многостраничный документ'),('document_expiry_changed','Изменён срок документа')]
    ACTIONS += [('work_reported','Ежедневное выполнение работ'),('work_decided','Проверка ежедневного выполнения')]
    ACTIONS += [('agreement_updated','Изменён договор с контрагентом')]
    ACTIONS += [('mailbox_updated','Корпоративный почтовый ящик'),('mail_prepared','Подготовлено письмо'),('mail_sent','Результат отправки письма'),('mail_read','Просмотр корпоративной почты')]
    ACTIONS += [('document_deleted','Документ удалён'),('object_deleted','Удалён пустой участок'),('admission_updated','Редакция комплекта допуска'),('customer_published','Опубликованы сведения заказчику')]
    actor=models.ForeignKey(User,null=True,on_delete=models.PROTECT,related_name='+')
    subject=models.ForeignKey(User,null=True,on_delete=models.PROTECT,related_name='+')
    action=models.CharField(max_length=60,choices=ACTIONS)
    details=models.JSONField(default=dict)
    created_at=models.DateTimeField(auto_now_add=True)

class LoginAttempt(models.Model):
    key=models.CharField(max_length=64,unique=True)
    count=models.PositiveIntegerField(default=0)
    window_start=models.DateTimeField()


class Preference(models.Model):
    user=models.OneToOneField(User,on_delete=models.CASCADE,related_name='preferences')
    hints=models.BooleanField('Показывать подсказки',default=True)
    notify_documents=models.BooleanField('Уведомления о новых документах',default=True)
    notify_versions=models.BooleanField('Уведомления о новых версиях',default=True)
    notify_comments=models.BooleanField('Уведомления об обращении внимания',default=True)
    large_text=models.BooleanField('Увеличенный текст',default=False)
    compact=models.BooleanField('Компактный список',default=False)

class Reminder(models.Model):
    user=models.ForeignKey(User,on_delete=models.CASCADE)
    document=models.ForeignKey('documents.Document',on_delete=models.PROTECT,null=True,blank=True)
    date=models.DateField('Напомнить')
    text=models.CharField('О чём напомнить',max_length=300)
    done=models.BooleanField(default=False)

class Favorite(models.Model):
    user=models.ForeignKey(User,on_delete=models.CASCADE)
    kind=models.CharField(max_length=20,choices=[('document','Документ'),('bundle','Комплект'),('object','Объект'),('contract','Договор')])
    entity_id=models.UUIDField()
    class Meta:
        constraints=[models.UniqueConstraint(fields=['user','kind','entity_id'],name='unique_favorite')]

class SupportRequest(models.Model):
    author=models.ForeignKey(User,on_delete=models.PROTECT)
    text=models.TextField('Что не получилось / предложение',max_length=3000)
    status=models.CharField('Статус',max_length=20,choices=[('new','Новое'),('progress','В работе'),('done','Решено')],default='new')
    response=models.TextField('Ответ',max_length=3000,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)

class UserSession(models.Model):
    user=models.ForeignKey(User,on_delete=models.CASCADE,related_name='active_sessions')
    session_key=models.CharField(max_length=40,unique=True)
    device=models.CharField(max_length=200,blank=True)
    last_seen=models.DateTimeField(auto_now=True)

class BusinessNoticeRead(models.Model):
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE)
    event=models.ForeignKey(AuditEvent,on_delete=models.CASCADE)
    read_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['user','event'],name='business_notice_read_unique')]
