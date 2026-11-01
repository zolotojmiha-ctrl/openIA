from decimal import Decimal
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404,render,redirect
from .access import prepare_required,visible_jobs
from .construction import planned_parts,COUPLING_METHODS
from .models import METHODS
from . import plans

class ConstructionForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    method=forms.ChoiceField(label='Тип конструкции',choices=METHODS)
    length_m=forms.DecimalField(label='Требуемая длина, м',max_digits=9,decimal_places=3,min_value=Decimal('.001'),required=False)
    wall_mm=forms.DecimalField(label='Толщина стенки, мм',min_value=1,max_digits=4,decimal_places=0,required=False)
    composition=forms.CharField(label='Состав, м (только для нескольких муфт)',max_length=200,required=False)
    reason=forms.CharField(label='Основание изменения',max_length=1000,widget=forms.Textarea(attrs={'rows':2,'placeholder':'Например: после обследования нужна конструкция 1500 мм'}))
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        wall=self.initial.get('wall_mm')
        if wall is not None and Decimal(str(wall))==Decimal(str(wall)).to_integral_value():self.initial['wall_mm']=int(wall)
        if self.initial.get('method') in COUPLING_METHODS and self.initial.get('composition'):
            try:
                _,parts=planned_parts(self.initial.get('length_m'),self.initial['composition'],self.initial['method'])
                if len(parts)==1:self.initial['composition']=''
            except ValidationError:pass
    def clean(self):
        c=super().clean()
        if c.get('method') in COUPLING_METHODS and not self.errors:
            try:c['length_m'],_=planned_parts(c.get('length_m'),c.get('composition',''),c['method'])
            except ValidationError as e:self.add_error('composition' if c.get('composition') else 'length_m',e)
        return c

@prepare_required
def revise(request,pk):
    job=get_object_or_404(visible_jobs(request.user),pk=pk)
    form=ConstructionForm(request.POST or None,initial={**{k:getattr(job,k) for k in plans.FIELDS},'expected_version':job.version})
    if request.method=='POST' and form.is_valid():
        try:
            job,released=plans.revise(request.user,job.pk,form.cleaned_data['expected_version'],{k:form.cleaned_data[k] for k in plans.FIELDS},form.cleaned_data['reason'])
        except ValidationError as e:form.add_error(None,e)
        else:
            messages.success(request,'Параметры задания сохранены.'+(' Неподходящее закрепление снято, сама муфта осталась на складе. Выберите изделие на новый размер.' if released else ''))
            return redirect('repair_detail',pk=job.pk)
    return render(request,'repairs/construction.html',{'form':form,'job':job})
