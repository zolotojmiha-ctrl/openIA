from uuid import UUID
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render,redirect,get_object_or_404
from django.utils import timezone
from accounts.permissions import permission_required,permitted
from documents.models import Document
from documents.access import visible_documents
from .models import Equipment,ProjectObject,Contract,EquipmentAssignment,EquipmentDocument
from .access import visible_objects,visible_contracts,contract_card
from . import equipment as service

DATE=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'})
class EquipmentForm(forms.ModelForm):
    expected_version=forms.IntegerField(widget=forms.HiddenInput,required=False)
    class Meta:
        model=Equipment
        fields=service.FIELDS
        widgets={'notes':forms.Textarea(attrs={'rows':3})}

class PlacementForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    project_object=forms.ModelChoiceField(label='Объект',queryset=ProjectObject.objects.none())
    contract=forms.ModelChoiceField(label='Контракт (при необходимости)',queryset=Contract.objects.none(),required=False)
    start_date=forms.DateField(label='Начало работы',widget=DATE)
    end_date=forms.DateField(label='Окончание работы',widget=DATE,required=False)
    note=forms.CharField(label='Пояснение',max_length=1000,required=False,widget=forms.Textarea(attrs={'rows':2}))
    transfer=forms.BooleanField(label='Перевод: завершить прежнее открытое назначение накануне новой даты',required=False)
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['project_object'].queryset=visible_objects(user).exclude(status='archived')
        self.fields['contract'].queryset=visible_contracts(user).exclude(status='archived')
        # Contract labels respect per-field grants.
        self.fields['contract'].label_from_instance=lambda c:contract_card(c,user)['title']

class EndForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    end_date=forms.DateField(label='Последний день работы',widget=DATE)

class FileForm(forms.ModelForm):
    expected_version=forms.IntegerField(widget=forms.HiddenInput,required=False)
    class Meta:
        model=EquipmentDocument
        fields=['document','label','expires_on','remind','days_before','active']
        labels={'document':'Файл документа','active':'Показывать связь с техникой'}
        widgets={'expires_on':DATE}
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs);self.fields['document'].queryset=visible_documents(user).filter(archived=False).order_by('title');self.fields['days_before'].min_value=0;self.fields['days_before'].max_value=365
        if self.instance.pk and not self.instance._state.adding:self.fields['document'].disabled=True

def filtered(user,params):
    qs=service.visible_equipment(user).order_by('name','inventory_number','pk')
    if params.get('archive')=='1' or params.get('state')=='archived':qs=qs.filter(status='archived')
    else:qs=qs.exclude(status='archived')
    q=str(params.get('q','')).strip()[:150]
    if q:qs=qs.filter(Q(name__icontains=q)|Q(inventory_number__icontains=q)|Q(registration_number__icontains=q)|Q(category__icontains=q))
    if params.get('state') in dict(Equipment._meta.get_field('status').choices):qs=qs.filter(status=params['state'])
    if params.get('object'):
        from uuid import UUID
        try:pk=UUID(str(params['object']))
        except ValueError:return qs.none()
        qs=qs.filter(pk__in=service.current(service.placements(user)).filter(project_object_id=pk).values('equipment_id'))
    return qs

@login_required
def listing(request):
    try:target_id=UUID(request.GET.get('assign_object',''))
    except ValueError:target_id=None
    target=visible_objects(request.user).filter(pk=target_id).first() if target_id else None
    qs=filtered(request.user,request.GET);page=Paginator(qs,25).get_page(request.GET.get('page'))
    location_map={}
    for row in service.current(service.placements(request.user)).filter(equipment__in=page.object_list):location_map.setdefault(row.equipment_id,[]).append(row)
    rows=[{'item':item,'locations':location_map.get(item.pk,[])} for item in page.object_list]
    return render(request,'projects/equipment_list.html',{'page':page,'rows':rows,'objects':visible_objects(request.user),'q':request.GET.get('q',''),'selected_object':request.GET.get('object',''),'state':request.GET.get('state',''),'target_object':target,'archive':request.GET.get('archive')=='1' or request.GET.get('state')=='archived','states':Equipment._meta.get_field('status').choices,'due':service.due_documents(request.user)})

@login_required
def detail(request,pk):
    item=get_object_or_404(service.visible_equipment(request.user),pk=pk)
    cards={c.pk:contract_card(c,request.user) for c in visible_contracts(request.user)}
    history=[{'row':a,'contract':cards.get(a.contract_id)} for a in service.placements(request.user).filter(equipment=item).order_by('-start_date','-pk')]
    return render(request,'projects/equipment_detail.html',{'item':item,'history':history,'files':service.equipment_files(request.user).filter(equipment=item),'events':[]})

@permission_required('equipment.manage')
def edit(request,pk=None):
    item=get_object_or_404(service.visible_equipment(request.user),pk=pk) if pk else None
    form=EquipmentForm(request.POST or None,instance=item,initial={'expected_version':item.version} if item else None)
    status=200
    if request.method=='POST' and form.is_valid():
        try:saved=service.save_equipment(request.user,pk,form.cleaned_data.get('expected_version'),form.cleaned_data)
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:messages.success(request,'Карточка техники сохранена.');return redirect('equipment_detail',pk=saved.pk)
    return render(request,'projects/equipment_form.html',{'form':form,'item':item,'title':'Изменить технику' if item else 'Добавить технику'},status=status)

@permission_required('equipment.manage')
def assign(request,pk):
    item=get_object_or_404(service.visible_equipment(request.user),pk=pk)
    initial={'expected_version':item.version,'start_date':timezone.localdate()}
    if request.GET.get('object'):initial['project_object']=request.GET['object']
    form=PlacementForm(request.POST or None,user=request.user,initial=initial);status=200
    if request.method=='POST' and form.is_valid():
        c=form.cleaned_data
        try:service.assign(request.user,pk,c['expected_version'],c['project_object'],c['contract'],c['start_date'],c['end_date'],c['note'],c['transfer'])
        except ValidationError as e:form.add_error(None,e);status=409
        else:messages.success(request,'Назначение сохранено. Доступ к объекту и документам не изменён.');return redirect('equipment_detail',pk=pk)
    return render(request,'projects/equipment_form.html',{'form':form,'item':item,'title':'Назначить / перевести технику'},status=status)

@permission_required('equipment.manage')
def end(request,pk):
    row=get_object_or_404(service.placements(request.user),pk=pk)
    form=EndForm(request.POST or None,initial={'expected_version':row.version,'end_date':timezone.localdate()});status=200
    if request.method=='POST' and form.is_valid():
        try:service.end_assignment(request.user,pk,form.cleaned_data['expected_version'],form.cleaned_data['end_date'])
        except ValidationError as e:form.add_error(None,e);status=409
        else:return redirect('equipment_detail',pk=row.equipment_id)
    return render(request,'projects/equipment_form.html',{'form':form,'item':row.equipment,'title':'Завершить назначение'},status=status)

@permission_required('equipment.manage')
def file_edit(request,equipment_id,pk=None):
    item=get_object_or_404(service.visible_equipment(request.user),pk=equipment_id)
    row=get_object_or_404(service.equipment_files(request.user),pk=pk,equipment=item) if pk else None
    form=FileForm(request.POST or None,instance=row,user=request.user,initial={'expected_version':row.version} if row else None);status=200
    if request.method=='POST' and form.is_valid():
        try:service.save_file(request.user,equipment_id,pk,form.cleaned_data.get('expected_version'),form.cleaned_data)
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:return redirect('equipment_detail',pk=equipment_id)
    return render(request,'projects/equipment_form.html',{'form':form,'item':item,'title':'Документ техники','document_form':True},status=status)
