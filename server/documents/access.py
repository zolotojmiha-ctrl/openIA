from django.db.models import Exists, OuterRef, Q
from projects.access import visible_objects, visible_contracts
from projects.models import COMPANY_TAX_ID
from .models import Document, DocumentAccess, DocumentObject, DocumentContract

def has_permission(user,permission):
    return user.is_authenticated and user.is_active and (user.is_leader or user.grants.filter(permission=permission).exists())

def scoped_documents(user):
    qs=Document.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID)
    if user.is_leader:return qs
    if not has_permission(user,'document.read'):return qs.none()
    bad_objects=DocumentObject.objects.filter(document_id=OuterRef('pk')).exclude(project_object_id__in=visible_objects(user).values('pk'))
    bad_contracts=DocumentContract.objects.filter(document_id=OuterRef('pk')).exclude(contract_id__in=visible_contracts(user).values('pk'))
    has_objects=DocumentObject.objects.filter(document_id=OuterRef('pk'))
    qs=qs.annotate(_bad_objects=Exists(bad_objects),_bad_contracts=Exists(bad_contracts),_has_objects=Exists(has_objects)).filter(_bad_objects=False,_bad_contracts=False)
    # General company files without an object are not opened by an incidental grant.
    qs=qs.filter(Q(_has_objects=True)|Q(kind='personal',people_linked=user))
    if not has_permission(user,'document.personal'):qs=qs.exclude(kind='personal')
    if not has_permission(user,'document.subcontractor'):qs=qs.exclude(kind='subcontractor')
    return qs.distinct()

def visible_documents(user):
    qs=scoped_documents(user)
    if user.is_leader:return qs
    deny=DocumentAccess.objects.filter(document_id=OuterRef('pk'),user=user,enabled=False)
    allow=DocumentAccess.objects.filter(document_id=OuterRef('pk'),user=user,enabled=True)
    return qs.annotate(_deny=Exists(deny),_allow=Exists(allow)).filter(_deny=False).filter(Q(_allow=True)|Q(created_by=user,kind='work'))

def version_visible(user,document,version):
    if version.document_id!=document.pk:return False
    if user.is_leader or document.current_version_id==version.pk:return True
    if document.created_by_id==user.pk and version.uploaded_by_id==user.pk:return True
    return document.access_rules.filter(user=user,enabled=True,can_history=True).exists()

def can_download(user,document,version):
    if not has_permission(user,'document.download') or not version_visible(user,document,version):return False
    if user.is_leader:return True
    if document.archived or version.status=='revision':return False
    if document.created_by_id==user.pk and version.uploaded_by_id==user.pk:return True
    return document.access_rules.filter(user=user,enabled=True,can_download=True).exists()

def can_modify(user,document,permission):
    return has_permission(user,permission) and (user.is_leader or document.section==user.role)
