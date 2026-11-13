from django.db.models import Q
from django.core.exceptions import PermissionDenied
from .models import Mailbox

def boxes(user):
    qs=Mailbox.objects.all()
    if not user.is_authenticated or not user.is_active or user.role=='customer' and not user.is_leader:return qs.none()
    if not user.is_leader:
        # Subquery avoids duplicate badges and keeps the outer row lockable on PostgreSQL.
        allowed=qs.filter(Q(owner=user)|Q(person__user=user,person__archived=False)|Q(members=user)).exclude(state='disabled')
        qs=qs.filter(pk__in=allowed.values('pk'))
    return qs.select_related('owner','person').prefetch_related('members')

def leader(user):
    if not user.is_active or not user.is_leader:raise PermissionDenied
