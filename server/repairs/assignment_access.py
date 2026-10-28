from django.core.exceptions import PermissionDenied
from accounts.models import User
from accounts.views import audit
from projects.models import ObjectAccess,ContractAccess

def has_access(master,section,contract):
    return master.is_leader or (ObjectAccess.objects.filter(user=master,project_object_id=section.project_object_id).exists() and ContractAccess.objects.filter(user=master,contract=contract).exists())

def grant(actor,master,section,contract):
    if not master or not contract:return
    master=User.objects.select_for_update().get(pk=master.pk)
    if not actor.is_leader or not master.is_active or (not master.is_leader and master.role!='foreman'):raise PermissionDenied
    if master.is_leader:return
    ObjectAccess.objects.get_or_create(user=master,project_object_id=section.project_object_id,defaults={'granted_by':actor})
    ContractAccess.objects.get_or_create(user=master,contract=contract,defaults={'granted_by':actor,'show_number':True,'show_subject':True})
    audit(actor,'repair_prepared',entity_id=str(master.pk),kind='master_assignment_access',contract=str(contract.pk),area=str(section.project_object_id))
