"""The register and the all-by-filter review use exactly the same access scope."""
import uuid
from decimal import Decimal,InvalidOperation
from django.core.exceptions import ValidationError
from django.db.models import Q
from .access import visible_jobs,can_prepare
from .values import kilometre
from .models import Job

FILTER_KEYS=('contract','object','section','master','q','state','year','legacy','archive')
def filter_values(params):return {k:str(params.get(k,''))[:150] for k in FILTER_KEYS}
def filtered_jobs(user,params,apply_state=True):
    archived=params.get('archive')=='1' and can_prepare(user)
    qs=visible_jobs(user,include_archived=archived)
    if archived:qs=qs.filter(defects__archived_at__isnull=False).distinct()
    for key,field in [('contract','contract_id'),('object','section__project_object_id'),('section','section_id'),('master','assigned_to_id')]:
        if params.get(key):
            try:qs=qs.filter(**{field:uuid.UUID(str(params[key]))})
            except (ValueError,TypeError):return qs.none()
    if params.get('legacy')=='1':qs=qs.filter(contract__isnull=True)
    q=str(params.get('q','')).strip()[:150]
    if q:
        search=Q(defects__number__icontains=q,defects__archived_at__isnull=not archived)|Q(defects__pipe_section__icontains=q,defects__archived_at__isnull=not archived)|Q(section__name__icontains=q)|Q(section__pipeline__icontains=q)
        try:
            km=kilometre(q);search|=Q(defects__kilometre__gte=max(Decimal(0),km-Decimal('.0005')),defects__kilometre__lt=km+Decimal('.0005'),defects__archived_at__isnull=not archived)
        except (ValidationError,InvalidOperation):pass
        qs=qs.filter(search).distinct()
    year=str(params.get('year',''))
    if year.isdigit() and 1900<=int(year)<=2200:qs=qs.filter(planned_date__year=int(year))
    if apply_state:
        state=params.get('state','')
        if state=='problems':qs=qs.exclude(problem='').exclude(state='unrepairable')
        elif state=='revision':qs=qs.exclude(review_note='')
        elif state in dict(Job._meta.get_field('state').choices):qs=qs.filter(state=state)
        elif state=='materials':qs=qs.filter(materials='missing')
    return qs
