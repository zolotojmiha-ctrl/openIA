"""Explicit delegation; job titles alone never grant administrative rights."""
from functools import wraps
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

def permitted(user,key):
    if not user.is_authenticated or not user.is_active:return False
    if user.is_leader:return True
    if user.role=='customer':return False
    return user.grants.filter(permission=key).exists()

def permission_required(key):
    def decorate(view):
        @login_required
        @wraps(view)
        def wrapped(request,*args,**kwargs):
            if not permitted(request.user,key):raise PermissionDenied
            return view(request,*args,**kwargs)
        return wrapped
    return decorate

def manageable_user(actor,user):
    if user.is_leader:return False
    if actor.is_leader:return True
    # A delegate cannot reset/block an account holding powers they do not hold.
    return not user.grants.exclude(permission__in=actor.grants.values('permission')).exists()

def can_link_person(user):
    return permitted(user,'users.manage') and permitted(user,'personnel.manage')
