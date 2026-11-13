from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET
from django.urls import reverse
from accounts.views import audit
from . import completion_register as register


@register.reader_required
@require_GET
def completed(request):
    form=register.Filters(request.user,request.GET)
    rows=register.queryset(request.user,form)
    params=request.GET.copy();params.pop('page',None);params.pop('download',None)
    query=params.urlencode()
    if request.GET.get('download')=='xlsx':
        if not form.is_valid():return HttpResponse('Исправьте фильтры перед скачиванием.',status=400)
        if rows.count()>10000:return HttpResponse('Выберите меньший период или участок: до 10000 работ в одном файле.',status=400)
        groups=register.groups(request.user,rows)
        if sum(g['count'] for g in groups)>10000:return HttpResponse('Выберите меньший период или участок: до 10000 дефектов в одном файле.',status=400)
        from .completion_xlsx import workbook
        response=HttpResponse(workbook(register.export_rows(groups)),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition']='attachment; filename="armada-completed-defects.xlsx"'
        response['Cache-Control']='private, no-store';response['X-Content-Type-Options']='nosniff'
        audit(request.user,'repair_exported',kind='actual_results',jobs=len(groups),defects=sum(g['count'] for g in groups))
        return response
    page=Paginator(rows,25).get_page(request.GET.get('page'))
    return render(request,'repairs/completed.html',{'form':form,'groups':register.groups(request.user,page),
        'page':page,'query':query,'summary':register.summary(rows) if form.is_valid() else None,
        'return_to':request.get_full_path(),'export_url':reverse('repair_completed')+'?'+query+('&' if query else '')+'download=xlsx'})
