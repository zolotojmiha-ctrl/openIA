from decimal import Decimal,InvalidOperation
from django.contrib.auth.decorators import login_required
from django.db.models import Q,Prefetch
from django.http import HttpResponse
from django.core.exceptions import ValidationError
from .access import visible_jobs,prepare_required,can_prepare
from .models import Report
from .values import kilometre
from .excel import workbook
from accounts.views import audit

MIME='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

def download(rows,filename):
    response=HttpResponse(workbook(rows),content_type=MIME)
    response['Content-Disposition']=f'attachment; filename="{filename}"'
    response['Cache-Control']='private, no-store'
    response['X-Content-Type-Options']='nosniff'
    return response

@prepare_required
def template(request):
    return download([],'openIA-defects-template.xlsx')

@login_required
def export(request):
    archived=request.GET.get('archive')=='1' and can_prepare(request.user)
    qs=visible_jobs(request.user,include_archived=archived)
    if archived:qs=qs.filter(defects__archived_at__isnull=False).distinct()
    if request.GET.get('contract'):
        import uuid
        try:qs=qs.filter(contract_id=uuid.UUID(request.GET['contract']))
        except (ValueError,TypeError):return HttpResponse('Некорректный контракт.',status=400)
    if request.GET.get('section'):
        import uuid
        try:qs=qs.filter(section_id=uuid.UUID(request.GET['section']))
        except (ValueError,TypeError):return HttpResponse('Некорректный участок.',status=400)
    if request.GET.get('object'):
        import uuid
        try:qs=qs.filter(section__project_object_id=uuid.UUID(request.GET['object']))
        except (ValueError,TypeError):return HttpResponse('Некорректный объект.',status=400)
    q=request.GET.get('q','').strip()[:150]
    if q:
        search=Q(defects__number__icontains=q,defects__archived_at__isnull=not archived)|Q(defects__pipe_section__icontains=q,defects__archived_at__isnull=not archived)|Q(section__name__icontains=q)|Q(section__pipeline__icontains=q)
        try:
            km=kilometre(q)
            search|=Q(defects__kilometre__gte=max(Decimal(0),km-Decimal('.0005')),defects__kilometre__lt=km+Decimal('.0005'),defects__archived_at__isnull=not archived)
        except (InvalidOperation,ValidationError):pass
        qs=qs.filter(search).distinct()
    year=request.GET.get('year','')
    if year.isdigit() and 1900<=int(year)<=2200:qs=qs.filter(planned_date__year=year)
    state=request.GET.get('state','')
    if state=='problems':qs=qs.exclude(problem='')
    elif state=='materials':qs=qs.filter(materials='missing')
    elif state in ('planned','progress','partial','reported','confirmed'):qs=qs.filter(state=state)
    master=request.GET.get('master','')
    if master:
        import uuid
        try:qs=qs.filter(assigned_to_id=uuid.UUID(master))
        except (ValueError,TypeError):return HttpResponse('Некорректный мастер.',status=400)
    selected=list(qs.order_by('section_id','created_at','pk').prefetch_related(Prefetch('reports',queryset=Report.objects.order_by('-created_at','-pk'),to_attr='export_reports'))[:301])
    if len(selected)>300:return HttpResponse('Для экспорта выберите участок или мастера: за один раз до 300 ремонтов.',status=400)
    rows=[]
    for job in selected:
        result=next((r for r in job.export_reports if r.kind in ('complete','partial','correction')),None)
        coordinates=f'{result.latitude}, {result.longitude}' if result and result.latitude is not None and result.longitude is not None else ''
        for d in (job.archived_defects if archived else job.active_defects):
            rows.append([job.repair_label,d.km,d.pipe_section,d.number,job.get_method_display(),job.length_m,job.wall_display if job.wall_display not in ('—','Требует уточнения') else '',job.assigned_to.phone if job.assigned_to_id else '',str(job.section_id),job.section.project_object.name,job.section.name,job.defect_status,coordinates,job.reason_display,job.problem_note,str(job.contract_id or '')])
    audit(request.user,'repair_exported',jobs=len(selected),defects=len(rows))
    return download(rows,'openIA-archived-defects.xlsx' if archived else 'openIA-defects.xlsx')
