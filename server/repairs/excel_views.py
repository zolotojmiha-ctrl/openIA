from decimal import Decimal,InvalidOperation
from django.contrib.auth.decorators import login_required
from django.db.models import Q,Prefetch
from django.http import HttpResponse
from django.core.exceptions import ValidationError
from .access import visible_jobs,prepare_required
from .models import Report
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
    qs=visible_jobs(request.user)
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
        search=Q(defects__number__icontains=q)|Q(defects__pipe_section__icontains=q)|Q(section__name__icontains=q)|Q(section__pipeline__icontains=q)
        try:
            km=Decimal(q.replace(',','.'))
            if km.is_finite() and 0<=km<=999999:search|=Q(defects__kilometre=km)
        except InvalidOperation:pass
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
        for d in job.defects.all():
            rows.append([job.repair_label,d.km,d.pipe_section,d.number,job.get_method_display(),job.length_m,job.wall_mm,job.assigned_to.phone if job.assigned_to_id else '',str(job.section_id),job.section.project_object.name,job.section.name,job.get_state_display(),coordinates,job.get_problem_display() if job.problem else '',job.problem_note])
    audit(request.user,'repair_exported',jobs=len(selected),defects=len(rows))
    return download(rows,'openIA-defects.xlsx')
