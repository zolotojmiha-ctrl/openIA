from django.db.models import Q
from django.core.exceptions import PermissionDenied
from .models import Mailbox

def boxes(user):
    qs=Mailbox.objects.all()
    if not user.is_authenticated or not user.is_active:return qs.none()
    if not user.is_leader:qs=qs.filter(Q(owner=user)|Q(person__user=user,person__archived=False)).exclude(state='disabled')
    return qs.select_related('owner','person')

def leader(user):
    if not user.is_active or not user.is_leader:raise PermissionDenied
