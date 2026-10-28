"""Assign equipment without leaving the contract/work-area context."""
from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404,render,redirect
from django.urls import reverse
from django.utils import timezone
from accounts.permissions import permission_required
from .access import visible_objects,visible_contracts,contract_card
from . import equipment
from .crews import visible_assignments
from .models import Equipment,BrigadeAssignment

class EquipmentPicker(forms.Form):
    snapshot=forms.CharField(widget=forms.HiddenInput)
    equipment=forms.ModelMultipleChoiceField(label='Выберите одну или несколько машин',queryset=Equipment.objects.none(),widget=forms.CheckboxSelectMultiple)
    crew=forms.ModelChoiceField(label='Бригада (необязательно)',queryset=BrigadeAssignment.objects.none(),required=False)
    start_date=forms.DateField(label='С какого числа',initial=timezone.localdate,widget=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}))
    end_date=forms.DateField(label='По какое число (необязательно)',required=False,widget=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}))
    transfer=forms.BooleanField(label='Перевести выбранные машины: завершить прежнее открытое назначение накануне указанной даты',required=False)
    def __init__(self,*args,user,area,contract,**kwargs):
        super().__init__(*args,**kwargs)
        assigned=equipment.current(equipment.placements(user)).filter(project_object=area,contract=contract).values('equipment_id')
        qs=equipment.visible_equipment(user).exclude(status='archived').exclude(pk__in=assigned).order_by('name','inventory_number')
        self.fields['equipment'].queryset=qs
        self.fields['equipment'].label_from_instance=lambda e:f'{e.name} · {e.registration_number or "без госномера"} · инв. {e.inventory_number}'
        self.fields['crew'].queryset=visible_assignments(user).filter(project_object=area,contract=contract).filter(end_date__isnull=True)|visible_assignments(user).filter(project_object=area,contract=contract,end_date__gte=timezone.localdate())
        self.initial['snapshot']=signing.dumps({'user':str(user.pk),'area':str(area.pk),'contract':str(contract.pk),'versions':{str(e.pk):e.version for e in qs}},salt='equipment-picker',compress=True)

@permission_required('equipment.manage')
def pick(request,contract_id,object_id):
    contract=get_object_or_404(visible_contracts(request.user).exclude(status='archived'),pk=contract_id)
    area=get_object_or_404(visible_objects(request.user).filter(contracts=contract).exclude(status='archived'),pk=object_id)
    back=reverse('object_detail',args=[area.pk])+'?contract='+str(contract.pk)+'#object-equipment'
    initial={}
    if request.GET.get('crew'):
        from uuid import UUID
        try:crew=visible_assignments(request.user).filter(pk=UUID(request.GET['crew']),project_object=area,contract=contract).first()
        except ValueError:crew=None
        if crew:initial={'crew':crew.pk,'start_date':max(timezone.localdate(),crew.start_date),'end_date':crew.end_date}
    form=EquipmentPicker(request.POST or None,user=request.user,area=area,contract=contract,initial=initial)
    if request.method=='POST' and form.is_valid():
        try:
            c=form.cleaned_data;expected=signing.loads(c['snapshot'],salt='equipment-picker',max_age=7200)
            if any(expected.get(k)!=v for k,v in {'user':str(request.user.pk),'area':str(area.pk),'contract':str(contract.pk)}.items()):raise ValidationError('Выбор устарел. Обновите страницу.')
            selected=list(c['equipment'].order_by('pk'))
            if len(selected)>100:raise ValidationError('Выберите не больше 100 машин за один раз.')
            with transaction.atomic():
                for item in selected:
                    version=expected.get('versions',{}).get(str(item.pk))
                    if version is None:raise ValidationError('Список техники изменился. Обновите страницу.')
                    try:equipment.assign(request.user,item.pk,version,area,contract,c['start_date'],c['end_date'],transfer=c['transfer'],crew_assignment=c['crew'])
                    except ValidationError as error:raise ValidationError(item.name+' · '+item.inventory_number+': '+'; '.join(error.messages))
        except (ValidationError,signing.BadSignature) as error:form.add_error(None,error if isinstance(error,ValidationError) else 'Список устарел. Обновите страницу.')
        else:
            messages.success(request,f'Назначено машин: {len(selected)}.');return redirect(back)
    return render(request,'projects/equipment_picker.html',{'form':form,'area':area,'card':contract_card(contract,request.user),'back':back})
