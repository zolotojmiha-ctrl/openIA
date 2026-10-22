import uuid
from decimal import Decimal
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404,render,redirect
from django.utils import timezone
from accounts.permissions import permission_required
from accounts.views import audit
from documents.access import sharing_users
from repairs.models import Coupling,Job,METHODS
from repairs.access import visible_jobs
from .models import Warehouse,WarehouseAccess,StockItem,StockBalance,STOCK_KINDS
from .access import visible_objects
from .crews import visible_assignments
from . import stock

DATE=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'})
class WarehouseForm(forms.ModelForm):
    expected_version=forms.IntegerField(widget=forms.HiddenInput,required=False)
    class Meta:
        model=Warehouse;fields=['name','location','project_object','brigade_assignment','archived']
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs);self.fields['project_object'].queryset=visible_objects(user);self.fields['brigade_assignment'].queryset=visible_assignments(user)

class ItemForm(forms.ModelForm):
    expected_version=forms.IntegerField(widget=forms.HiddenInput,required=False)
    method=forms.ChoiceField(label='Метод ремонта / тип муфты',choices=[('','—'),*METHODS[:5]],required=False)
    class Meta:
        model=StockItem;fields=[*stock.ITEM_FIELDS,'coupling']
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs);self.fields['coupling'].queryset=Coupling.objects.filter(project_object__in=visible_objects(user))
        self.fields['coupling'].help_text='Необязательно. Конкретная муфта списывается автоматически при установке. Остальные позиции учитываются количеством.'
        if not self.instance._state.adding:self.fields['coupling'].disabled=True

class MoveForm(forms.Form):
    token=forms.UUIDField(widget=forms.HiddenInput,initial=uuid.uuid4)
    item=forms.ModelChoiceField(label='Материал / муфта',queryset=StockItem.objects.none())
    kind=forms.ChoiceField(label='Операция',choices=STOCK_KINDS[:-1])
    quantity=forms.DecimalField(label='Количество',max_digits=18,decimal_places=3,min_value=Decimal('0.001'))
    source=forms.ModelChoiceField(label='Откуда',queryset=Warehouse.objects.none(),required=False)
    destination=forms.ModelChoiceField(label='Куда',queryset=Warehouse.objects.none(),required=False)
    job=forms.ModelChoiceField(label='Работа для списания',queryset=Job.objects.none(),required=False)
    date=forms.DateField(label='Дата',widget=DATE,initial=timezone.localdate)
    note=forms.CharField(label='Основание / пояснение',max_length=1500,widget=forms.Textarea(attrs={'rows':2}))
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs);self.fields['item'].queryset=stock.items(user).filter(archived=False).order_by('sku')
        for k in ('source','destination'):self.fields[k].queryset=stock.warehouses(user).filter(archived=False).order_by('name')
        self.fields['job'].queryset=visible_jobs(user)
        self.fields['job'].label_from_instance=lambda j:f'{j.section.name} · '+', '.join('км '+d.km+' № '+d.number for d in j.active_defects)

class ReverseForm(forms.Form):
    token=forms.UUIDField(widget=forms.HiddenInput,initial=uuid.uuid4)
    note=forms.CharField(label='Причина отмены',max_length=1500,widget=forms.Textarea(attrs={'rows':3}))

@login_required
def home(request):
    qs=stock.warehouses(request.user).filter(archived=request.GET.get('archive')=='1').order_by('name')
    return render(request,'projects/stock_home.html',{'warehouses':qs,'archive':request.GET.get('archive')=='1'})

@login_required
def detail(request,pk):
    wh=get_object_or_404(stock.warehouses(request.user),pk=pk)
    rows=StockBalance.objects.filter(warehouse=wh,quantity__gt=0).select_related('item').order_by('item__sku')
    history=stock.movements(request.user).filter(Q(source=wh)|Q(destination=wh)).order_by('-created_at','-pk')
    return render(request,'projects/stock_detail.html',{'warehouse':wh,'rows':rows,'page':Paginator(history,30).get_page(request.GET.get('page')),'object_visible':wh.project_object_id and visible_objects(request.user).filter(pk=wh.project_object_id).exists()})

@permission_required('stock.manage')
def warehouse_edit(request,pk=None):
    wh=get_object_or_404(stock.warehouses(request.user),pk=pk) if pk else None
    form=WarehouseForm(request.POST or None,user=request.user,instance=wh,initial={'expected_version':wh.version} if wh else None);status=200
    if request.method=='POST' and form.is_valid():
        try:row=stock.save_warehouse(request.user,pk,form.cleaned_data.get('expected_version'),form.cleaned_data)
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:return redirect('stock_detail',pk=row.pk)
    return render(request,'projects/stock_form.html',{'form':form,'warehouse':wh,'title':'Изменить склад / архив' if wh else 'Добавить склад или место выдачи бригаде'},status=status)

@permission_required('stock.manage')
def item_list(request):
    qs=stock.items(request.user).filter(archived=request.GET.get('archive')=='1').order_by('sku')
    return render(request,'projects/stock_items.html',{'page':Paginator(qs,50).get_page(request.GET.get('page')),'archive':request.GET.get('archive')=='1'})

@permission_required('stock.manage')
def item_edit(request,pk=None):
    item=get_object_or_404(stock.items(request.user),pk=pk) if pk else None
    form=ItemForm(request.POST or None,user=request.user,instance=item,initial={'expected_version':item.version} if item else None);status=200
    if request.method=='POST' and form.is_valid():
        try:stock.save_item(request.user,pk,form.cleaned_data.get('expected_version'),form.cleaned_data)
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:return redirect('stock_items')
    return render(request,'projects/stock_form.html',{'form':form,'title':'Изменить номенклатуру' if item else 'Добавить материал / муфту'},status=status)

@permission_required('stock.manage')
def movement(request):
    initial={}
    for k in ('source','destination','item'):
        try:initial[k]=uuid.UUID(request.GET.get(k,''))
        except ValueError:pass
    if request.GET.get('kind') in dict(STOCK_KINDS[:-1]):initial['kind']=request.GET['kind']
    form=MoveForm(request.POST or None,user=request.user,initial=initial);status=200
    if request.method=='POST' and form.is_valid():
        c=form.cleaned_data
        try:row=stock.move(request.user,c['item'].pk,c['kind'],c['quantity'],c['source'].pk if c['source'] else None,c['destination'].pk if c['destination'] else None,c['date'],c['note'],c['token'],c['job'].pk if c['job'] else None)
        except ValidationError as e:form.add_error(None,e);status=409
        else:messages.success(request,'Операция сохранена. Остатки обновлены.');return redirect('stock_detail',pk=row.destination_id or row.source_id)
    return render(request,'projects/stock_form.html',{'form':form,'title':'Складская операция','movement':True},status=status)

@permission_required('stock.manage')
def reversal(request,pk):
    row=get_object_or_404(stock.movements(request.user),pk=pk);form=ReverseForm(request.POST or None);status=200
    if request.method=='POST' and form.is_valid():
        try:stock.reverse(request.user,pk,form.cleaned_data['note'],form.cleaned_data['token'])
        except ValidationError as e:form.add_error(None,e);status=409
        else:return redirect('stock_detail',pk=row.destination_id or row.source_id)
    return render(request,'projects/stock_form.html',{'form':form,'title':'Отменить складскую операцию','original':row},status=status)

class AccessForm(forms.Form):
    employee=forms.ModelChoiceField(label='Сотрудник',queryset=None)
    enabled=forms.BooleanField(label='Разрешить просмотр склада',required=False)
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs);self.fields['employee'].queryset=sharing_users(user).filter(is_active=True)
        self.fields['employee'].label_from_instance=lambda u:u.full_name

@permission_required('stock.manage')
def access(request,pk):
    wh=get_object_or_404(stock.warehouses(request.user),pk=pk);form=AccessForm(request.POST or None,user=request.user)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            actor=stock.manager(request.user)
            if form.cleaned_data['enabled']:WarehouseAccess.objects.get_or_create(warehouse=wh,user=form.cleaned_data['employee'],defaults={'granted_by':actor})
            else:WarehouseAccess.objects.filter(warehouse=wh,user=form.cleaned_data['employee']).delete()
            audit(actor,'stock_access_changed',warehouse=str(wh.pk),employee=str(form.cleaned_data['employee'].pk),enabled=form.cleaned_data['enabled'])
        return redirect('stock_access',pk=pk)
    return render(request,'projects/stock_form.html',{'form':form,'warehouse':wh,'title':'Просмотр склада сотрудниками','access_rules':wh.access_rules.select_related('user')})
