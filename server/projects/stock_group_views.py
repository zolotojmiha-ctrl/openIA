from collections import defaultdict
from decimal import Decimal
import uuid
from django.utils import timezone
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import render,redirect
from accounts.permissions import permission_required
from repairs.access import visible_jobs
from repairs.models import Job
from . import stock_groups as service,stock_planning as planning

class DistributionRow(forms.Form):
    job=forms.ModelChoiceField(label='Участок, нефтепровод, км и дефекты',queryset=Job.objects.none(),required=False)
    quantity=forms.IntegerField(label='Количество, шт.',min_value=0,required=False)
    def __init__(self,*args,user,group=None,**kwargs):
        super().__init__(*args,**kwargs)
        from django.db.models import Q
        active=visible_jobs(user).exclude(state__in=['reported','confirmed']).exclude(contract__status='archived').values('pk')
        held=[d['job'].pk for d in group['distribution']] if group else []
        self.fields['job'].queryset=visible_jobs(user,include_archived=True).filter(Q(pk__in=active)|Q(pk__in=held))
        self.fields['job'].label_from_instance=planning.job_label
    def clean(self):
        c=super().clean()
        if c.get('quantity') and not c.get('job'):self.add_error('job','Выберите работу для этого количества.')
        return c

@permission_required('stock.manage')
def distribute(request):
    token=request.POST.get('group','') if request.method=='POST' else request.GET.get('group','')
    try:group=service.current(request.user,token)
    except ValidationError as e:
        messages.error(request,'; '.join(e.messages));return redirect('stock_inventory')
    initial=[{'job':d['job'].pk,'quantity':d['quantity']} for d in group['distribution'] if d['state']=='reserved']
    Set=forms.formset_factory(DistributionRow,extra=6,max_num=50,validate_max=True,absolute_max=60)
    formset=Set(request.POST or None,initial=initial,form_kwargs={'user':request.user,'group':group});status=200
    if request.method=='POST' and formset.is_valid():
        targets=defaultdict(int)
        for f in formset:
            if f.cleaned_data.get('job'):targets[str(f.cleaned_data['job'].pk)]+=f.cleaned_data.get('quantity') or 0
        try:service.distribute(request.user,token,targets)
        except ValidationError as e:messages.error(request,'; '.join(e.messages));status=409
        else:messages.success(request,'Распределение сохранено. Остаток склада не изменился.');return redirect('stock_inventory')
    return render(request,'projects/stock_distribution.html',{'group':group,'token':token,'formset':formset},status=status)

class TransferForm(forms.Form):
    destination=forms.ModelChoiceField(label='На какой склад',queryset=None)
    quantity=forms.DecimalField(label='Количество',min_value=Decimal('.001'),max_digits=18,decimal_places=3)
    date=forms.DateField(label='Дата перемещения',widget=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}),initial=timezone.localdate)
    note=forms.CharField(label='Основание',max_length=1200,initial='Перемещение между складами')
    include_reserved=forms.BooleanField(label='Переместить также закреплённые муфты, сохранив назначение',required=False)
    token=forms.UUIDField(widget=forms.HiddenInput,initial=uuid.uuid4)
    def __init__(self,*args,user,group,**kwargs):
        super().__init__(*args,**kwargs)
        from . import stock
        self.fields['destination'].queryset=stock.warehouses(user).filter(archived=False).exclude(pk=group['warehouse'].pk)

@permission_required('stock.manage')
def transfer(request):
    token=request.POST.get('group','') if request.method=='POST' else request.GET.get('group','')
    try:group=service.current(request.user,token)
    except ValidationError as e:messages.error(request,'; '.join(e.messages));return redirect('stock_inventory')
    if not group['warehouse']:messages.error(request,'Нет исходного склада.');return redirect('stock_inventory')
    form=TransferForm(request.POST or None,user=request.user,group=group);status=200
    if request.method=='POST' and form.is_valid():
        c=form.cleaned_data
        try:service.transfer(request.user,token,c['destination'].pk,c['quantity'],c['date'],c['note'],c['token'],c['include_reserved'])
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:messages.success(request,'Перемещение сохранено целиком.');return redirect('stock_detail',pk=c['destination'].pk)
    return render(request,'projects/stock_transfer.html',{'form':form,'group':group,'token':token},status=status)

class QuantityForm(forms.Form):
    quantity=forms.DecimalField(label='Фактическое количество на этом складе',min_value=0,max_digits=18,decimal_places=3)
    note=forms.CharField(label='Причина исправления',max_length=1200)

@permission_required('stock.manage')
def edit(request):
    token=request.POST.get('group','') if request.method=='POST' else request.GET.get('group','')
    try:group=service.current(request.user,token)
    except ValidationError as e:messages.error(request,'; '.join(e.messages));return redirect('stock_inventory')
    form=QuantityForm(request.POST or None,initial={'quantity':group['quantity']});status=200
    if request.method=='POST' and form.is_valid():
        c=form.cleaned_data
        try:service.correct_quantity(request.user,token,c['quantity'],c['note'])
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:messages.success(request,'Количество исправлено на выбранном складе.');return redirect('stock_inventory')
    return render(request,'projects/stock_group_edit.html',{'group':group,'form':form,'token':token},status=status)
