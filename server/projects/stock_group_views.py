from collections import defaultdict
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
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['job'].queryset=visible_jobs(user).exclude(state__in=['reported','confirmed']).exclude(contract__status='archived')
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
    formset=Set(request.POST or None,initial=initial,form_kwargs={'user':request.user});status=200
    if request.method=='POST' and formset.is_valid():
        targets=defaultdict(int)
        for f in formset:
            if f.cleaned_data.get('job'):targets[str(f.cleaned_data['job'].pk)]+=f.cleaned_data.get('quantity') or 0
        try:service.distribute(request.user,token,targets)
        except ValidationError as e:messages.error(request,'; '.join(e.messages));status=409
        else:messages.success(request,'Распределение сохранено. Остаток склада не изменился.');return redirect('stock_inventory')
    return render(request,'projects/stock_distribution.html',{'group':group,'token':token,'formset':formset},status=status)
