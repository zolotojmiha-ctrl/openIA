import uuid
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404,render,redirect
from django.urls import reverse
from accounts.permissions import permission_required
from .models import EquipmentMaintenancePart
from .equipment import visible_equipment
from . import equipment_parts


class PartForm(forms.ModelForm):
    expected_version=forms.IntegerField(widget=forms.HiddenInput,required=False)
    token=forms.UUIDField(widget=forms.HiddenInput)
    class Meta:
        model=EquipmentMaintenancePart
        fields=equipment_parts.FIELDS
        widgets={'note':forms.Textarea(attrs={'rows':3})}
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.initial.update(expected_version=self.instance.version if not self.instance._state.adding else 0,token=self.instance.creation_token)
        self.fields['article'].help_text='Сохраните номер полностью, включая начальные нули, буквы и дефисы. Поиск откроется по производителю и артикулу.'
        self.fields['quantity'].help_text='Необязательно. Это справочная потребность на одно ТО, без списания со склада.'


@permission_required('equipment.manage')
def edit(request,equipment_id,pk=None):
    equipment=get_object_or_404(visible_equipment(request.user),pk=equipment_id)
    part=get_object_or_404(EquipmentMaintenancePart,equipment=equipment,pk=pk) if pk else None
    form=PartForm(request.POST or None,instance=part)
    status=200
    if request.method=='POST' and form.is_valid():
        try:equipment_parts.save(request.user,equipment.pk,part.pk if part else None,form.cleaned_data.get('expected_version'),form.cleaned_data,form.cleaned_data['token'])
        except ValidationError as exc:form.add_error(None,exc);status=409
        else:
            messages.success(request,'Запчасть для ТО сохранена.')
            return redirect(reverse('equipment_detail',args=[equipment.pk])+'#maintenance-parts')
    return render(request,'projects/equipment_part_form.html',{'form':form,'equipment':equipment,'part':part},status=status)
