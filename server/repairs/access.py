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
from projects.access import visible_objects,visible_contracts
from .models import Job, Section, Coupling

def visible_sections(user):
    return Section.objects.filter(project_object__in=visible_objects(user))

def visible_jobs(user,include_archived=False):
    qs=Job.objects.filter(section__in=visible_sections(user)).filter(Q(contract__isnull=True)|Q(contract__in=visible_contracts(user)))
    if not include_archived:
        # Keep outer queries DISTINCT-free: PostgreSQL must be able to lock jobs.
        qs=qs.filter(pk__in=Job.objects.filter(defects__archived_at__isnull=True).values('pk'))
    if user.role=='subcontractor':
        from projects.models import SubcontractOrder
        from projects.subcontracts import own_order_filter
        own=SubcontractOrder.objects.filter(own_order_filter(user)).values('pk')
        qs=qs.filter(Q(subcontract_order__in=own)|Q(subcontract_order__isnull=True,assigned_to=user))
    elif not can_prepare(user):qs=qs.filter(assigned_to=user)
    return qs.select_related('subcontract_order__company','crew_assignment','crew_assignment__brigade','contract','section','section__project_object','assigned_to').prefetch_related('defects')

def selectable_couplings(job,include_historical=False):
    available=Q(installation__isnull=True)
    if not include_historical:available&=Q(source__historical_installation=False)|~Q(source__has_key='historical_installation')
    return Coupling.objects.filter(project_object=job.section.project_object).filter(Q(stock_item__isnull=True)|Q(stock_item__deleted_at__isnull=True)).filter(Q(stock_item__reserved_job__isnull=True)|Q(stock_item__reserved_job=job)).filter(available|Q(installation__job=job)).order_by('method','length_mm','serial')


def reportable_jobs(user):
    qs=visible_jobs(user).exclude(contract__status='archived')
    if user.is_leader:return qs
    from django.utils import timezone
    today=timezone.localdate()
    if user.role=='subcontractor':
        from projects.models import SubcontractOrder
        from projects.subcontracts import own_order_filter
        return qs.filter(Q(subcontract_order__in=SubcontractOrder.objects.filter(own_order_filter(user,report=True)).exclude(contract__status='archived').exclude(project_object__status='archived').values('pk'))|Q(subcontract_order__isnull=True,assigned_to=user))
    return qs.filter(assigned_to=user).filter(Q(crew_assignment__isnull=True)|Q(crew_assignment__start_date__lte=today)&(Q(crew_assignment__end_date__isnull=True)|Q(crew_assignment__end_date__gte=today)))


def repair_contracts(user):
    # JSON containment is not available on SQLite; keep the same choices on both databases.
    return visible_contracts(user).filter(pk__in=[c.pk for c in visible_contracts(user) if 'repairs' in c.work_kinds and c.status!='archived'])

def validate_contract_place(contract,section):
    from django.core.exceptions import ValidationError
    if not contract or contract.status=='archived' or 'repairs' not in contract.work_kinds:
        raise ValidationError('Выберите действующий контракт с видом работ «Устранение дефектов».')
    if not contract.objects_linked.filter(pk=section.project_object_id).exists():
        raise ValidationError('Участок не относится к месту выполнения этого контракта.')

def visible_batches(user):
    from .models import ImportBatch
    return ImportBatch.objects.filter(section__in=visible_sections(user)).filter(Q(contract__isnull=True)|Q(contract__in=visible_contracts(user)))
