import uuid
from django.conf import settings
from django.db import models
from accounts.models import ROLES

SECTIONS=[('contracts','Договоры и приложения'),('personnel','Документы персонала'),('journals','Журналы'),('as_built','Исполнительная документация'),('acts','Акты и сметы'),('drawings','Чертежи и схемы'),('media','Фото и видео'),('general','Прочие документы')]
KINDS=[('work','Рабочий документ'),('personal','Персональный документ'),('subcontractor','Документ субподрядчика')]

class Document(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    legal_entity=models.ForeignKey('projects.LegalEntity',on_delete=models.PROTECT)
    title=models.CharField('Название',max_length=200)
    number=models.CharField('Номер',max_length=100,blank=True)
    date=models.DateField('Дата документа',null=True,blank=True)
    section=models.CharField('Профессиональный раздел',max_length=20,choices=SECTIONS)
    kind=models.CharField('Категория доступа',max_length=20,choices=KINDS,default='work')
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    responsible=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name='responsible_documents')
    created_at=models.DateTimeField(auto_now_add=True)
    archived=models.BooleanField(default=False)
    revision=models.PositiveIntegerField(default=1)
    bundle=models.ForeignKey('Bundle',on_delete=models.PROTECT,null=True,blank=True,related_name='documents')
    is_primary=models.BooleanField('Основной документ комплекта',default=False)
    link_not_required=models.BooleanField('Связь не требуется',default=False)
    personnel_linked=models.ManyToManyField('Person',blank=True,related_name='documents')
    current_version=models.ForeignKey('DocumentVersion',on_delete=models.PROTECT,null=True,blank=True,related_name='+')
    objects_linked=models.ManyToManyField('projects.ProjectObject',through='DocumentObject',related_name='documents')
    contracts_linked=models.ManyToManyField('projects.Contract',through='DocumentContract',related_name='documents')
    people_linked=models.ManyToManyField(settings.AUTH_USER_MODEL,through='DocumentPerson',related_name='personnel_documents')
    def __str__(self):return self.title

class DocumentVersion(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    document=models.ForeignKey(Document,on_delete=models.PROTECT,related_name='versions')
    sequence=models.PositiveIntegerField()
    storage_key=models.CharField(max_length=40,unique=True)
    original_name=models.CharField(max_length=200)
    name_key=models.CharField(max_length=400,blank=True)
    content_type=models.CharField(max_length=100)
    size=models.PositiveIntegerField()
    sha256=models.CharField(max_length=64)
    uploaded_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    created_at=models.DateTimeField(auto_now_add=True)
    comment=models.CharField('Комментарий',max_length=2000,blank=True)
    status=models.CharField(max_length=20,choices=[('available','Доступна'),('pending','Ожидает утверждения'),('approved','Утверждена'),('revision','На доработке — использование запрещено')],default='available')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['document','sequence'],name='unique_document_sequence')]
        ordering=['-sequence']
    def save(self,*args,**kwargs):
        if not self._state.adding:
            old=type(self).objects.get(pk=self.pk)
            immutable=['document_id','sequence','storage_key','original_name','name_key','content_type','size','sha256','uploaded_by_id','comment','created_at']
            if any(getattr(self,key)!=getattr(old,key) for key in immutable):
                raise ValueError('Содержимое и сведения существующей версии неизменяемы.')
        return super().save(*args,**kwargs)

class ApprovalEvent(models.Model):
    version=models.OneToOneField(DocumentVersion,on_delete=models.PROTECT,related_name='decision')
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    decision=models.CharField(max_length=20,choices=[('approved','Утверждена'),('revision','На доработку')])
    reason=models.CharField(max_length=2000,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)

class DocumentAccess(models.Model):
    document=models.ForeignKey(Document,on_delete=models.PROTECT,related_name='access_rules')
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='document_access')
    enabled=models.BooleanField(default=False)
    can_download=models.BooleanField(default=False)
    can_history=models.BooleanField(default=False)
    can_replace=models.BooleanField(default=False)
    granted_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    updated_at=models.DateTimeField(auto_now=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['document','user'],name='unique_document_user_access')]

class DocumentObject(models.Model):
    document=models.ForeignKey(Document,on_delete=models.PROTECT)
    project_object=models.ForeignKey('projects.ProjectObject',on_delete=models.PROTECT)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['document','project_object'],name='unique_document_object')]

class DocumentContract(models.Model):
    document=models.ForeignKey(Document,on_delete=models.PROTECT)
    contract=models.ForeignKey('projects.Contract',on_delete=models.PROTECT)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['document','contract'],name='unique_document_contract')]

class DocumentPerson(models.Model):
    document=models.ForeignKey(Document,on_delete=models.PROTECT)
    person=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['document','person'],name='unique_document_person')]


class Bundle(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    title=models.CharField('Название комплекта',max_length=200)
    legal_entity=models.ForeignKey('projects.LegalEntity',on_delete=models.PROTECT)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    revision=models.PositiveIntegerField(default=1)
    created_at=models.DateTimeField(auto_now_add=True)
    def __str__(self):return self.title

class BundleAccess(models.Model):
    bundle=models.ForeignKey(Bundle,on_delete=models.PROTECT,related_name='access_rules')
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    enabled=models.BooleanField(default=True)
    can_replace=models.BooleanField(default=False)
    granted_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['bundle','user'],name='unique_bundle_user')]

class DocumentRead(models.Model):
    document=models.ForeignKey(Document,on_delete=models.CASCADE,related_name='reads')
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE)
    version=models.ForeignKey(DocumentVersion,on_delete=models.PROTECT)
    read_at=models.DateTimeField(auto_now=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['document','user'],name='unique_document_read')]

class Person(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    legal_entity=models.ForeignKey('projects.LegalEntity',on_delete=models.PROTECT)
    full_name=models.CharField('ФИО',max_length=150)
    position=models.CharField('Должность',max_length=150,blank=True)
    user=models.OneToOneField(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name='person_card')
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    archived=models.BooleanField(default=False)
    revision=models.PositiveIntegerField(default=1)
    def __str__(self):return self.full_name

class PersonAssignment(models.Model):
    person=models.ForeignKey(Person,on_delete=models.PROTECT,related_name='assignments')
    project_object=models.ForeignKey('projects.ProjectObject',on_delete=models.PROTECT,related_name='personnel')
    start_date=models.DateField('Начало',null=True,blank=True)
    end_date=models.DateField('Окончание',null=True,blank=True)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)

class Comment(models.Model):
    document=models.ForeignKey(Document,on_delete=models.PROTECT,related_name='comments')
    author=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    text=models.TextField('Комментарий',max_length=2000)
    created_at=models.DateTimeField(auto_now_add=True)

class Notice(models.Model):
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE)
    document=models.ForeignKey(Document,on_delete=models.PROTECT)
    comment=models.ForeignKey(Comment,on_delete=models.PROTECT)
    read=models.BooleanField(default=False)
    created_at=models.DateTimeField(auto_now_add=True)

class UploadReceipt(models.Model):
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    token=models.UUIDField()
    document=models.ForeignKey(Document,on_delete=models.PROTECT)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['user','token'],name='unique_upload_receipt')]
