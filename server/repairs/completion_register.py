"""Read-only actual results: one row per defect, one measurement per repair."""
from decimal import Decimal, InvalidOperation
from functools import wraps
from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import OuterRef, Subquery
from accounts.models import User
from projects.access import visible_contracts, visible_objects, contract_card
from .access import can_prepare, visible_jobs, visible_sections
from .filters import filtered_jobs
from .models import Report, Defect
from projects.scope_widgets import ScopedContracts, ScopeChecklist, values


def permitted(user):
    return user.is_authenticated and user.is_active and user.role not in ('customer','subcontractor') and (user.is_leader or user.role=='pto' or can_prepare(user))


def reader_required(view):
    @login_required
    @wraps(view)
    def wrapped(request,*args,**kwargs):
        if not permitted(request.user):raise PermissionDenied
        return view(request,*args,**kwargs)
    return wrapped


class MasterChoice(forms.ModelChoiceField):
    def label_from_instance(self,obj):return obj.full_name


class Filters(forms.Form):
    contract=ScopedContracts(label='Контракты',queryset=None,required=False,widget=ScopeChecklist,help_text='Если ничего не выбрано — все доступные.')
    object=forms.ModelMultipleChoiceField(label='Объекты',queryset=None,required=False,widget=ScopeChecklist,help_text='Если ничего не выбрано — все доступные.')
    section=forms.ModelChoiceField(label='Участок нефтепровода',queryset=None,required=False)
    master=MasterChoice(label='Ответственный мастер',queryset=None,required=False)
    start=forms.DateField(label='Выполнено с',required=False,widget=forms.DateInput(attrs={'type':'date'},format='%Y-%m-%d'))
    end=forms.DateField(label='По',required=False,widget=forms.DateInput(attrs={'type':'date'},format='%Y-%m-%d'))
    state=forms.ChoiceField(label='Проверка',required=False,choices=[('','Все устранённые'),('reported','На проверке руководителя'),('confirmed','Проверено руководителем')])
    q=forms.CharField(label='Км, № дефекта или секции',required=False,max_length=150)

    def __init__(self,user,data=None,*args,**kwargs):
        if data is not None:
            original=data;data=data.copy()
            for name in ('contract','object'):
                selected=values(original,name)
                if hasattr(data,'setlist'):data.setlist(name,selected)
                else:data[name]=selected
        super().__init__(data,*args,**kwargs)
        self.fields['contract'].user=user
        self.fields['contract'].queryset=visible_contracts(user).order_by('number')
        self.fields['object'].queryset=visible_objects(user).order_by('name')
        self.fields['section'].queryset=visible_sections(user).order_by('rnu','pipeline','name')
        self.fields['master'].queryset=User.objects.filter(pk__in=visible_jobs(user).exclude(assigned_to__isnull=True).values('assigned_to_id')).order_by('full_name')
        for name in ('section','master'):self.fields[name].empty_label='Все доступные'

    def clean(self):
        data=super().clean()
        if data.get('start') and data.get('end') and data['start']>data['end']:
            raise forms.ValidationError('Начало периода не может быть позже окончания.')
        return data


def queryset(user,form):
    if not form.is_valid():return visible_jobs(user).none()
    data=form.cleaned_data
    params={key:str(getattr(data.get(key),'pk',data.get(key)) or '') for key in ('section','master','state','q')}
    params.update({key:','.join(str(row.pk) for row in data[key]) for key in ('contract','object')})
    rows=filtered_jobs(user,params).filter(state__in=['reported','confirmed'])
    latest=Report.objects.filter(job_id=OuterRef('pk'),kind__in=['complete','partial','correction']).order_by('-created_at','-pk')
    rows=rows.annotate(result_id=Subquery(latest.values('pk')[:1]),result_date=Subquery(latest.values('performed_date')[:1]))
    # No planned date substitution. Legacy records without a result remain visible
    # until a date filter is selected, and measurements stay explicitly absent.
    if data.get('start'):rows=rows.filter(result_date__gte=data['start'])
    if data.get('end'):rows=rows.filter(result_date__lte=data['end'])
    return rows.order_by('section__project_object__name','section__name','result_date','pk')


def coating(report):
    if not report:return None
    try:
        value=Decimal(str(report.context.get('actual_coating_length_m') if isinstance(report.context,dict) else None))
        return value if value.is_finite() and value>0 else None
    except (ValueError,TypeError,InvalidOperation):return None


def summary(rows):
    total=Decimal(0);missing=0
    results=Report.objects.filter(pk__in=Subquery(rows.order_by().values('result_id')))
    for context in results.values_list('context',flat=True).iterator(chunk_size=500):
        try:value=Decimal(str(context.get('actual_coating_length_m') if isinstance(context,dict) else None))
        except (ValueError,TypeError,InvalidOperation):value=None
        if value is not None and value.is_finite() and value>0:total+=value
        else:missing+=1
    jobs=rows.count();reports=results.count()
    return {'jobs':jobs,'defects':Defect.objects.filter(archived_at__isnull=True,jobs__in=rows).distinct().count(),
            'coating':total,'missing':missing+jobs-reports}


def groups(user,jobs):
    jobs=list(jobs)
    reports={r.pk:r for r in Report.objects.filter(pk__in=[j.result_id for j in jobs if j.result_id]).select_related('author')}
    contracts={}
    result=[]
    for job in jobs:
        from .historical import result as historical_result
        report=historical_result(job,reports.get(job.result_id))
        defects=sorted(job.active_defects,key=lambda d:(d.kilometre,d.number,d.pipe_section))
        if job.contract_id not in contracts:
            contracts[job.contract_id]=contract_card(job.contract,user)['title'] if job.contract_id else 'Без контракта'
        result.append({'job':job,'report':report,'coating':coating(report),'defects':defects,'count':len(defects),
                       'contract':contracts[job.contract_id]})
    return result


HEADERS=['Контракт','Объект','Участок','Км','№ дефекта','№ секции','Код общей работы',
         'Дата выполнения','Ответственный мастер','Автор результата','Фактический метод ремонта',
         'Длина конструкции, м (один раз на работу)','Состав конструкции, м',
         'Фактическая длина покрытия, м (один раз на работу)','Проверка','Дата приёмки заказчиком','Основание приёмки']


def export_rows(groups):
    rows=[]
    for group in groups:
        job=group['job'];report=group['report']
        for index,defect in enumerate(group['defects']):
            rows.append([group['contract'],job.section.project_object.name,job.section.name,defect.km,defect.number,defect.pipe_section,
                         str(job.pk),report.performed_date.strftime('%d.%m.%Y') if report and report.performed_date else '',
                         job.assigned_to.full_name if job.assigned_to_id else '',report.author.full_name if report and report.author else '',
                         report.get_method_display() if report and report.method else '',
                         report.length_m if report and index==0 else None,report.composition if report else '',
                         group['coating'] if index==0 else None,job.verification_label,
                         job.accepted_date.strftime('%d.%m.%Y') if job.accepted_date else '',job.acceptance_basis])
    return rows
