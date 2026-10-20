from functools import wraps
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


def can_prepare(user):
    return user.is_authenticated and user.is_active and (user.is_leader or user.grants.filter(permission='repairs.prepare').exists())


def prepare_required(view):
    @login_required
    @wraps(view)
    def wrapped(request,*args,**kwargs):
        if not can_prepare(request.user):raise PermissionDenied
        return view(request,*args,**kwargs)
    return wrapped

from django.db.models import Q
from projects.access import visible_objects
from .models import Job, Section, Coupling

def visible_sections(user):
    return Section.objects.filter(project_object__in=visible_objects(user))

def visible_jobs(user):
    qs=Job.objects.filter(section__in=visible_sections(user))
    if not can_prepare(user):qs=qs.filter(assigned_to=user)
    return qs.select_related('section','section__project_object','assigned_to').prefetch_related('defects')

def selectable_couplings(job,include_historical=False):
    available=Q(installation__isnull=True)
    if not include_historical:available&=Q(source__historical_installation=False)|~Q(source__has_key='historical_installation')
    return Coupling.objects.filter(project_object=job.section.project_object).filter(available|Q(installation__job=job)).order_by('method','length_mm','serial')


def reportable_jobs(user):
    qs=visible_jobs(user)
    return qs if user.is_leader else qs.filter(assigned_to=user)
