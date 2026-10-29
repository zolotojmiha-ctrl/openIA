from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch
from django.http import HttpResponse
from django.core.exceptions import ValidationError
from .access import prepare_required,can_prepare
from .models import Report,METHODS
from .excel import workbook,SIMPLE_HEADERS
from accounts.views import audit

MIME='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

def download(rows,filename,headers=None):
    response=HttpResponse(workbook(rows,headers=headers,hidden_columns=() if headers else (0,8,15),column_choices={3:[label for _,label in METHODS]} if headers else None),content_type=MIME)
    response['Content-Disposition']=f'attachment; filename="{filename}"'
    response['Cache-Control']='private, no-store'
    response['X-Content-Type-Options']='nosniff'
    return response

@prepare_required
def template(request):
    return download([],'openIA-defects-template.xlsx',SIMPLE_HEADERS)

@login_required
def export(request):
    archived=request.GET.get('archive')=='1' and can_prepare(request.user)
    import uuid
    for key in ('contract','object','section','master'):
        if request.GET.get(key):
            try:uuid.UUID(request.GET[key])
            except (ValueError,TypeError):return HttpResponse('Некорректный параметр фильтра: '+key,status=400)
    from .filters import filtered_jobs,filter_values
    qs=filtered_jobs(request.user,filter_values(request.GET))
    selected=list(qs.order_by('section_id','created_at','pk').prefetch_related(Prefetch('reports',queryset=Report.objects.order_by('-created_at','-pk'),to_attr='export_reports'))[:10001])
    if len(selected)>10000:return HttpResponse('Для экспорта выберите участок или мастера: за один раз до 10000 ремонтов.',status=400)
    rows=[]
    for job in selected:
        result=next((r for r in job.export_reports if r.kind in ('complete','partial','correction')),None)
        coordinates=f'{result.latitude}, {result.longitude}' if result and result.latitude is not None and result.longitude is not None else ''
        for d in (job.archived_defects if archived else job.active_defects):
            rows.append([job.repair_label,d.km,d.pipe_section,d.number,job.get_method_display(),job.length_m,job.wall_display if job.wall_display not in ('—','Требует уточнения') else '',job.assigned_to.phone if job.assigned_to_id else '',str(job.section_id),job.section.project_object.name,job.section.name,job.defect_status,coordinates,job.reason_display,job.problem_note,str(job.contract_id or '')])
    audit(request.user,'repair_exported',jobs=len(selected),defects=len(rows))
    return download(rows,'openIA-archived-defects.xlsx' if archived else 'openIA-defects.xlsx')
