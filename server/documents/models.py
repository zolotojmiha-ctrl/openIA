import uuid
from django.conf import settings
from django.db import models
from accounts.models import ROLES

SECTIONS=[('general','Общие документы'),*ROLES]
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
    created_at=models.DateTimeField(auto_now_add=True)
    archived=models.BooleanField(default=False)
    revision=models.PositiveIntegerField(default=1)
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
    content_type=models.CharField(max_length=100)
    size=models.PositiveIntegerField()
    sha256=models.CharField(max_length=64)
    uploaded_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    created_at=models.DateTimeField(auto_now_add=True)
    comment=models.CharField('Комментарий',max_length=2000,blank=True)
    status=models.CharField(max_length=20,choices=[('pending','Ожидает утверждения'),('approved','Утверждена'),('revision','На доработке — использование запрещено')],default='pending')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['document','sequence'],name='unique_document_sequence')]
        ordering=['-sequence']
    def save(self,*args,**kwargs):
        if not self._state.adding:
            old=type(self).objects.get(pk=self.pk)
            immutable=['document_id','sequence','storage_key','original_name','content_type','size','sha256','uploaded_by_id','comment','created_at']
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
