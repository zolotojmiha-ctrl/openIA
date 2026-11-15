from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, render, redirect
from . import stock_reconciliation as service
from .installation_stock import candidates,source_key


class ReconciliationForm(forms.Form):
    def __init__(self,*args,job,**kwargs):
        super().__init__(*args,**kwargs)
        for c in service.pieces(job):
            choices=[('', 'Выбрать автоматически из свободного наличия')]
            for row in candidates(job,c.method,c.length_mm,c.wall_mm,c.serial):
                donor=row.item.reserved_job
                place=('резерв км '+', '.join(d.km for d in donor.active_defects)) if donor and donor.pk!=job.pk else 'свободно / собственный резерв'
                choices.append((source_key(row.item_id,row.warehouse_id),f'{row.warehouse.name} · {place} · {row.item.sku}'))
            self.fields[str(c.pk)]=forms.ChoiceField(label=f'{c.get_method_display()} {c.length_mm} × {c.wall_mm} мм · №{c.serial}',choices=choices,required=False)


@login_required
def reconcile(request,pk):
    job=get_object_or_404(service.allowed(request.user),pk=pk)
    form=ReconciliationForm(request.POST if request.method=='POST' else None,job=job);facts=None;token=''
    if request.method=='POST':
        try:
            if request.POST.get('stage')=='confirm':
                service.confirm(request.user,request.POST.get('preview',''))
                messages.success(request,'Недостающее списание проведено. Прежний отчёт и монтаж сохранены.')
                return redirect('repair_detail',pk=pk)
            if form.is_valid():facts,token=service.preview(request.user,pk,form.cleaned_data)
        except ValidationError as exc:form.add_error(None,exc)
    return render(request,'repairs/stock_reconciliation.html',{'job':job,'form':form,'facts':facts,'preview':token})


from django.views.decorators.http import require_POST
from django.http import JsonResponse
from django.utils.html import format_html,format_html_join
from .access import reportable_jobs
from .forms import ReportForm

@login_required
@require_POST
def sources(request,pk):
    job=get_object_or_404(reportable_jobs(request.user),pk=pk)
    form=ReportForm(request.POST,job=job,action='repaired',user=request.user)
    fields=getattr(form,'stock_source_fields',[])
    html=format_html_join('', '<p>{}{}<small>{}</small></p>',
                          ((field.label_tag(),field.as_widget(),field.help_text) for field in fields))
    return JsonResponse({'html':str(html)})
