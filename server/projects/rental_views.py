from finance.money_forms import MoneyForm,MoneyModelForm
from decimal import Decimal
import uuid
from decimal import Decimal
from datetime import timedelta
from django import forms
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404,redirect,render
from django.utils import timezone
from django.views.decorators.http import require_POST
from . import rentals
from .models import EquipmentAssignment, CounterpartyAgreement

DATE=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'})

class SetupForm(forms.Form):
    placement=forms.ModelChoiceField(label='Техника и назначение на объект',queryset=None)
    supplier=forms.CharField(label='Арендодатель',max_length=200)
    reference=forms.CharField(label='Номер / название договора аренды',max_length=200)
    agreement=forms.ModelChoiceField(label='Документ договора в системе (необязательно)',queryset=None,required=False)
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs)
        from .equipment import visible_equipment
        self.fields['placement'].queryset=EquipmentAssignment.objects.filter(equipment__in=visible_equipment(user),equipment__ownership='rent',rental__isnull=True).select_related('equipment','project_object')
        self.fields['placement'].label_from_instance=lambda p:f'{p.equipment.name} · {p.equipment.registration_number or "без госномера"} · {p.project_object.name} · с {p.start_date:%d.%m.%Y}'
        self.fields['agreement'].queryset=CounterpartyAgreement.objects.filter(kind='equipment_rent',archived=False)

class RentalInfoForm(forms.Form):
    version=forms.IntegerField(widget=forms.HiddenInput)
    supplier=forms.CharField(label='Арендодатель',max_length=200,required=False)
    reference=forms.CharField(label='Номер договора',max_length=200,required=False)
    agreement=forms.ModelChoiceField(label='Связать с сохранённым договором — необязательно',queryset=CounterpartyAgreement.objects.filter(kind='equipment_rent',archived=False),required=False)

class RateForm(MoneyForm):
    starts_on=forms.DateField(label='Тариф действует с',widget=DATE,initial=timezone.localdate)
    basis=forms.ChoiceField(label='Стоимость указана',choices=[('day','За день / смену'),('hour','За час')])
    work_price=forms.DecimalField(label='Работа — стоимость, ₽',min_value=0,max_digits=16,decimal_places=2)
    idle_price=forms.DecimalField(label='Наш простой — стоимость, ₽',min_value=0,max_digits=16,decimal_places=2)

class DayForm(forms.Form):
    date=forms.DateField(label='Дата',widget=DATE,initial=timezone.localdate)
    kind=forms.ChoiceField(choices=[('work','Работал — 10 ч'),('idle','Наш простой — 4 ч'),('external','Не по нашей вине — 0 ч')],widget=forms.HiddenInput)
    version=forms.IntegerField(min_value=0,initial=0,widget=forms.HiddenInput)
    note=forms.CharField(label='Причина исправления (только при изменении отметки)',max_length=500,required=False)

class PaymentForm(MoneyForm):
    date=forms.DateField(label='Дата перечисления',widget=DATE,initial=timezone.localdate)
    amount=forms.DecimalField(label='Сумма, ₽',min_value=Decimal('.01'),max_digits=18,decimal_places=2)
    note=forms.CharField(label='Основание: аванс / оплата, номер платежа',max_length=500)
    token=forms.UUIDField(widget=forms.HiddenInput,initial=uuid.uuid4)

class FinanceLinkForm(forms.Form):
    scope=forms.ModelChoiceField(label='Финансовая карточка контракта',queryset=None,required=False)
    def __init__(self,*args,rental,**kwargs):
        super().__init__(*args,**kwargs)
        from finance.models import FinancialScope
        from .models import COMPANY_TAX_ID
        self.fields['scope'].queryset=FinancialScope.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID,contract_id=rental.placement.contract_id)

class PeriodForm(forms.Form):
    start=forms.DateField(label='С',widget=DATE)
    end=forms.DateField(label='По включительно',widget=DATE)
    note=forms.CharField(label='Счёт / акт / пояснение',max_length=500,required=False)

@login_required
def listing(request):
    return redirect('/cabinet/equipment/?ownership=rent')

@login_required
def setup(request):
    if request.method=='GET':return redirect('/cabinet/equipment/?ownership=rent')
    if not rentals.manager(request.user):raise PermissionDenied
    form=SetupForm(request.POST or None,user=request.user)
    if request.method=='POST' and form.is_valid():
        try:rental=rentals.configure(request.user,form.cleaned_data['placement'].pk,**{k:v for k,v in form.cleaned_data.items() if k!='placement'})
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('rental_detail',pk=rental.pk)
    return render(request,'projects/operation_form.html',{'form':form,'title':'Включить учёт аренды','back':'/cabinet/rentals/','intro':'Сначала назначьте арендованную технику объекту и бригаде в её карточке. Затем здесь задаются договор и тариф.'})

@login_required
def detail(request,pk,embedded=False):
    rental=get_object_or_404(rentals.visible(request.user),pk=pk)
    can_manage=rentals.manager(request.user);financial=rentals.financial_reader(request.user);can_write=rentals.financial_writer(request.user)
    missing=rentals.missing_dates(rental,request.user,367)
    initial={'date':missing[0] if missing else timezone.localdate()}
    day=DayForm(initial=initial);rate=RateForm(initial={'starts_on':rental.placement.start_date if not rental.rates.exists() else timezone.localdate()});payment=PaymentForm();period=PeriodForm(initial={'start':rental.placement.start_date,'end':min(rental.placement.end_date or timezone.localdate(),timezone.localdate())})
    finance_link=FinanceLinkForm(rental=rental,initial={'scope':rental.financial_scope_id})
    info=RentalInfoForm(initial={'supplier':rental.supplier,'reference':rental.reference,'agreement':rental.agreement_id,'version':rental.version})
    if request.method=='POST':
        action=request.POST.get('action');form={'day':DayForm,'rate':RateForm,'payment':PaymentForm,'period':PeriodForm,'finance_link':FinanceLinkForm,'info':RentalInfoForm}.get(action)
        if form is None:raise PermissionDenied
        if action not in ('day','info') and not can_write:raise PermissionDenied
        bound=form(request.POST,**({'rental':rental} if action=='finance_link' else {}))
        if bound.is_valid():
            try:
                if action=='day':
                    saved=rentals.save_day(request.user,pk,**bound.cleaned_data)
                    messages.success(request,f'Отметка за {saved.date:%d.%m.%Y} сохранена: {saved.get_kind_display()}.')
                elif action=='rate':rentals.add_rate(request.user,pk,**{k:bound.cleaned_data[k] for k in ('starts_on','basis','work_price','idle_price')})
                elif action=='payment':rentals.payment(request.user,pk,**{k:bound.cleaned_data[k] for k in ('date','amount','note','token')})
                elif action=='info':rentals.update_info(request.user,pk,**bound.cleaned_data)
                elif action=='finance_link':rentals.link_finance(request.user,pk,**bound.cleaned_data)
                else:rentals.close_period(request.user,pk,**bound.cleaned_data)
            except ValidationError as e:bound.add_error(None,e)
            else:
                if embedded and action=='day':
                    from django.urls import reverse
                    from urllib.parse import urlencode
                    params={'rental':str(rental.pk)}
                    if request.GET.get('return_to'):params['return_to']=request.GET['return_to'][:4096]
                    return redirect(reverse('equipment_detail',args=[rental.placement.equipment_id])+'?'+urlencode(params))
                return redirect('equipment_detail',pk=rental.placement.equipment_id) if embedded else redirect('rental_detail',pk=pk)
        if action=='day':day=bound
        elif action=='rate':rate=bound
        elif action=='payment':payment=bound
        elif action=='period':period=bound
        elif action=='finance_link':finance_link=bound
        else:info=bound
    days=[d for d in rental.days.select_related('updated_by').prefetch_related('revisions__author').order_by('-date') if rentals.can_read_day(request.user,rental,d.date)][:100]
    for d in days:d.can_change=rentals.allowed_on(request.user,rental,d.date)
    chart=[]
    for kind,label in [('work','Работал'),('idle','Наш простой'),('external','Не по нашей вине')]:
        count=rental.days.filter(kind=kind).count() if financial or rentals.technical_reader(request.user) else sum(d.kind==kind for d in days)
        chart.append({'label':label,'count':count})
    high=max([d['count'] for d in chart]+[1])
    for bar in chart:bar['percent']=round(bar['count']/high*100)
    context={'can_record_rental':can_manage or bool(missing) or any(d.can_change for d in days),'chart':chart,'finance_link_form':finance_link,'can_write_rental_finance':can_write,'rental':rental,'days':days,'missing':missing,'day_form':day,'rate_form':rate,'payment_form':payment,'period_form':period,'can_manage_rental':can_manage,'rental_financial':financial,'total':rentals.totals(rental) if financial else None,'payments':rental.payments.all() if financial else [],'periods':rental.periods.all() if financial else []}
    context['info_form']=info
    if rentals.technical_reader(request.user):
        from django.db.models import Sum
        context['technical_hours']=rental.days.filter(date__lte=timezone.localdate()).aggregate(v=Sum('hours'))['v'] or 0
    if embedded:return context
    return render(request,'projects/rental_detail.html',context)

@login_required
@require_POST
def reverse(request,pk,payment_id):
    rental=get_object_or_404(rentals.visible(request.user),pk=pk)
    try:rentals.reverse_payment(request.user,pk,payment_id,request.POST.get('reason',''))
    except ValidationError as e:return HttpResponse('; '.join(e.messages),status=400)
    return redirect('rental_detail',pk=pk)

@login_required
def export(request,pk):
    rental=get_object_or_404(rentals.visible(request.user),pk=pk)
    from repairs.excel import workbook
    financial=rentals.financial_reader(request.user)
    from accounts.xlsx import money_value
    def thousands(value):return money_value(value,1000)
    headers=['Дата','Техника','Госномер','Объект','Состояние','Часы к оплате','Внёс']+(['Начислено, тыс. руб.'] if financial else [])
    rows=[]
    for d in rental.days.select_related('updated_by').order_by('date'):
        if not rentals.can_read_day(request.user,rental,d.date):continue
        rows.append([str(d.date),rental.placement.equipment.name,rental.placement.equipment.registration_number,rental.placement.project_object.name,d.get_kind_display(),d.hours,d.updated_by.full_name]+([thousands(d.amount) if d.rate_id or not d.hours else 'Тариф не задан'] if financial else []))
    sheets=[('Табель',headers,rows)]
    history=[]
    for d in rental.days.prefetch_related('revisions__author').filter(version__gt=1).order_by('date'):
        if not rentals.can_read_day(request.user,rental,d.date):continue
        for revision in d.revisions.all().order_by('number'):
            history.append([str(d.date),revision.number,revision.data.get('hours',''),revision.author.full_name,str(revision.created_at),revision.data.get('note','')]+([thousands(revision.data.get('amount') or 0)] if financial else []))
    sheets.append(('История отметок',['Дата','Редакция','Часы','Кто внёс','Когда','Причина']+(['Начислено, тыс. руб.'] if financial else []),history))
    if financial:
        sheets.append(('Платежи',['Дата','Сумма, тыс. руб.','Основание','Отменён'],[[str(p.date),thousands(p.amount),p.note,'Да' if p.reversed else 'Нет'] for p in rental.payments.all()]))
        totals=rentals.totals(rental);sheets.append(('Итог',['Показатель','Значение, тыс. руб.'],[[k,thousands(v)] for k,v in [('Начислено',totals['charged']),('Перечислено',totals['paid']),('К оплате',totals['debt']),('Остаток аванса',totals['advance'])]]))
        sheets.append(('Периоды сверки',['С','По','Состояние','Основание','Кто закрыл','Когда','Начислено на дату закрытия, тыс. руб.','Оплачено на дату закрытия, тыс. руб.','Долг на дату закрытия, тыс. руб.','Аванс на дату закрытия, тыс. руб.','Причина повторной сверки'],[[str(p.starts_on),str(p.ends_on),'Повторная сверка' if p.reopened_at else 'Закрыт',p.note,p.created_by.full_name,str(p.created_at),thousands(p.snapshot.get('charged') or 0),thousands(p.snapshot.get('paid') or 0),thousands(p.snapshot.get('debt') or 0),thousands(p.snapshot.get('advance') or 0),p.reopen_reason] for p in rental.periods.select_related('created_by')]))
    flat=[]
    for name,cols,data in sheets:
        flat.append([name]);flat.append(cols);flat.extend(data);flat.append([])
    response=HttpResponse(workbook(flat,headers=[],sheet_name='Учёт аренды'),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');response['Content-Disposition']='attachment; filename="equipment-rental.xlsx"';response['Cache-Control']='private, no-store';return response

@login_required
@require_POST
def reopen(request,pk,period_id):
    get_object_or_404(rentals.visible(request.user),pk=pk)
    try:rentals.reopen_period(request.user,pk,period_id,request.POST.get('reason',''))
    except ValidationError as e:return HttpResponse('; '.join(e.messages),status=400)
    return redirect('rental_detail',pk=pk)
