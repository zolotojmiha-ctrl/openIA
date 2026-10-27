from django.db.models import Exists, OuterRef, Q, F, Case, When, Value, BooleanField
from projects.models import COMPANY_TAX_ID
from .models import Document, DocumentAccess, Bundle, BundleAccess, DocumentRead

def has_permission(user,permission):
    return user.is_authenticated and user.is_active and (user.is_leader or user.grants.filter(permission=permission).exists())

def visible_bundles(user):
    qs=Bundle.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID)
    qs=qs.annotate(has_unread=Exists(unread_documents(user).filter(bundle_id=OuterRef('pk'))))
    if user.is_leader:return qs
    return qs.filter(Q(created_by=user)|Q(access_rules__user=user,access_rules__enabled=True)).distinct()

def scoped_documents(user):
    # File permission is explicit. Linking never grants a file or its parent card.
    return Document.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID,deleted_at__isnull=True)

def visible_documents(user):
    qs=scoped_documents(user)
    if user.role=='customer' and not user.is_leader:return qs.none()
    if not user.is_authenticated or not user.is_active:return qs.none()
    reads=DocumentRead.objects.filter(document_id=OuterRef('pk'),user=user,version_id=OuterRef('current_version_id'))
    qs=qs.annotate(_already_read=Exists(reads)).annotate(is_unread=Case(When(_already_read=True,then=Value(False)),When(current_version__uploaded_by=user,then=Value(False)),default=Value(True),output_field=BooleanField()))
    if user.is_leader:return qs
    rules=DocumentAccess.objects.filter(document_id=OuterRef('pk'),user=user)
    inherited=BundleAccess.objects.filter(bundle_id=OuterRef('bundle_id'),user=user,enabled=True)
    return qs.annotate(_deny=Exists(rules.filter(enabled=False)),_allow=Exists(rules.filter(enabled=True)),_bundle=Exists(inherited)).filter(
        Q(created_by=user)|Q(responsible=user)|Q(archived=False,_deny=False)&(Q(_allow=True)|Q(_bundle=True)|Q(bundle__created_by=user)))

def can_manage(user,document):
    return user.is_leader or document.created_by_id==user.pk or document.responsible_id==user.pk

def can_manage_bundle(user,bundle):
    return user.is_leader or bundle.created_by_id==user.pk

def version_visible(user,document,version):
    return version.document_id==document.pk

def can_download(user,document,version):
    return version_visible(user,document,version) and (not document.archived or can_manage(user,document))

def can_modify(user,document,permission):
    if can_manage(user,document):return True
    if document.archived:return False
    if permission!='document.replace':return False
    rule=document.access_rules.filter(user=user).first()
    if rule is not None:return rule.enabled and rule.can_replace
    return document.bundle_id is not None and BundleAccess.objects.filter(bundle_id=document.bundle_id,user=user,enabled=True,can_replace=True).exists()

def unread_documents(user):
    reads=DocumentRead.objects.filter(document_id=OuterRef('pk'),user=user,version_id=OuterRef('current_version_id'))
    return visible_documents(user).filter(archived=False,current_version__isnull=False).exclude(current_version__uploaded_by=user).annotate(_read=Exists(reads)).filter(_read=False)

def unsorted_documents(user):
    qs=visible_documents(user).filter(archived=False,link_not_required=False)
    return qs.filter(Q(section='contracts',contracts_linked__isnull=True)|Q(section='personnel',personnel_linked__isnull=True,people_linked__isnull=True)|Q(section__in=['journals','as_built'],objects_linked__isnull=True)).distinct()

def visible_people(user):
    from .models import Person
    qs=Person.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID)
    if user.is_leader or has_permission(user,'personnel.manage'):return qs
    # External participants never receive a directory of other contractors' personnel.
    if user.role=='subcontractor':return qs.filter(Q(created_by=user)|Q(user=user))
    # Viewing a file does not grant a whole personnel card.
    return qs.filter(Q(created_by=user)|Q(assignments__project_object__assignments__user=user)).distinct()


def sharing_users(user,document=None):
    from accounts.models import User
    qs=User.objects.filter(leader_slot__isnull=True)
    if user.is_leader:return qs
    if user.role=='subcontractor':return qs.none()
    base=qs.exclude(role='subcontractor')
    if isinstance(document,Document) and has_permission(user,'subcontract.manage') and can_manage(user,document):
        eligible=eligible_subcontract_users(user,document)
        prior=document.access_rules.values('user_id')
        return qs.filter(~Q(role='subcontractor')|Q(pk__in=eligible.values('pk'))|Q(pk__in=prior)).distinct()
    return base

def eligible_subcontract_users(user,document):
    from accounts.models import User
    from projects.subcontracts import files
    if not has_permission(user,'subcontract.manage') or not can_manage(user,document):return User.objects.none()
    ids=set()
    for link in files(user).filter(document=document,active=True,company__archived=False):
        reps=link.company.representatives.filter(active=True,user__is_active=True,user__role='subcontractor')
        if link.order_id:reps=reps.filter(order_grants__order_id=link.order_id,order_grants__active=True)
        ids.update(reps.values_list('user_id',flat=True))
    return User.objects.filter(pk__in=ids)

def can_share(user,document):
    return user.role!='subcontractor' and can_manage(user,document)
