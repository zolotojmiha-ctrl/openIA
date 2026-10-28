import uuid
from collections import Counter
from decimal import Decimal
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render, redirect
from django.utils import timezone
from accounts.permissions import permission_required, permitted
from repairs.access import visible_jobs
from repairs.models import Job, METHODS
from .models import StockItem
from .stock_views import ItemForm, DATE
from . import stock, stock_planning as planning

class ReceiptForm(ItemForm):
    catalogue_item=forms.ModelChoiceField(queryset=None,required=False,widget=forms.HiddenInput)
    catalogue_version=forms.IntegerField(required=False,widget=forms.HiddenInput)
    warehouse=forms.ModelChoiceField(label='На какой склад добавить (место хранения)',queryset=None)
    quantity=forms.DecimalField(label='Количество (муфты — отдельные изделия)',min_value=Decimal('.001'),max_digits=18,decimal_places=3,initial=1)
    date=forms.DateField(label='Дата получения',widget=DATE,initial=timezone.localdate)
    note=forms.CharField(label='Основание / поставка',max_length=1300,initial='Поступление на склад')
    token=forms.UUIDField(widget=forms.HiddenInput,initial=uuid.uuid4)
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,user=user,**kwargs)
        for key in ('sku','archived','coupling','expected_version'):self.fields.pop(key,None)
        self.fields['name'].required=False
        self.fields['name'].help_text='Для муфты можно оставить пустым: название составится из типа.'
        self.fields['catalogue_item'].queryset=stock.items(user).filter(archived=False)
        self.fields['warehouse'].queryset=stock.warehouses(user).filter(archived=False).order_by('name')
        self.fields['unit'].help_text='Муфты, композит и герметик — шт.; изоляция — м². Старые единицы не пересчитываются автоматически.'

@permission_required('stock.manage')
def receipt(request):
    initial={}
    if request.GET.get('warehouse'):
        from django.http import Http404
        try:wid=uuid.UUID(request.GET['warehouse'])
        except ValueError:raise Http404
        wh=get_object_or_404(stock.warehouses(request.user),pk=wid);initial['warehouse']=wh
    if request.GET.get('item'):
        from django.http import Http404
        try:iid=uuid.UUID(request.GET['item'])
        except ValueError:raise Http404
        item=get_object_or_404(stock.items(request.user),pk=iid)
        initial.update({k:getattr(item,k) for k in (*stock.ITEM_FIELDS[:-1],'serial','receipt_origin')})
        if not item.movements.exists() and not item.coupling_id:initial.update(catalogue_item=item,catalogue_version=item.version)
    form=ReceiptForm(request.POST or None,user=request.user,initial=initial);status=200
    if request.method=='POST' and form.is_valid():
        c=form.cleaned_data
        try:wh=planning.receive(request.user,c,c['warehouse'],c['quantity'],c['date'],c['note'],c['token'])
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:messages.success(request,'Поступление сохранено. Муфты добавлены поштучно в остатки.');return redirect('stock_detail',pk=wh.pk)
    return render(request,'projects/stock_form.html',{'form':form,'title':'Добавить материал / муфты на склад','receipt':True},status=status)

class AllocationForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    job=forms.ModelChoiceField(label='Закрепить за работой / дефектами',queryset=None,required=False,empty_label='Свободна — снять закрепление')
    serial=forms.CharField(label='Заводской № (необязательно)',max_length=100,required=False)
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['job'].queryset=visible_jobs(user).exclude(state__in=['reported','confirmed']).exclude(contract__status='archived')
        self.fields['job'].label_from_instance=planning.job_label

@permission_required('stock.manage')
def allocate(request,pk):
    item=get_object_or_404(stock.items(request.user),pk=pk)
    form=AllocationForm(request.POST or None,user=request.user,initial={'expected_version':item.version,'serial':item.serial or (item.coupling.serial if item.coupling_id else ''),'job':item.reserved_job_id});status=200
    if request.method=='POST' and form.is_valid():
        from django.db import transaction
        c=form.cleaned_data
        try:
            with transaction.atomic():
                values={k:getattr(item,k) for k in stock.ITEM_FIELDS};values.update(coupling=item.coupling,serial=c['serial'])
                updated=stock.save_item(request.user,item.pk,c['expected_version'],values)
                planning.reserve(request.user,pk,updated.version,c['job'].pk if c['job'] else None)
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:return redirect('stock_inventory')
    return render(request,'projects/stock_form.html',{'form':form,'title':'Заводской номер и назначение муфты','item_summary':item},status=status)

class DeleteForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    note=forms.CharField(label='Причина удаления ошибочной позиции',max_length=1200,widget=forms.Textarea)

@permission_required('stock.manage')
def delete(request,pk):
    item=get_object_or_404(stock.items(request.user),pk=pk);status=200
    form=DeleteForm(request.POST or None,initial={'expected_version':item.version})
    if request.method=='POST' and form.is_valid():
        try:planning.delete(request.user,pk,form.cleaned_data['expected_version'],form.cleaned_data['note'])
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:messages.success(request,'Позиция удалена из действующих списков. Остаток скорректирован, история сохранена.');return redirect('stock_inventory')
    return render(request,'projects/stock_form.html',{'form':form,'title':'Удалить ошибочную складскую позицию','item_summary':item,'deletion':True},status=status)

@permission_required('stock.manage')
def split(request,pk,warehouse_id):
    item=get_object_or_404(stock.items(request.user),pk=pk);form=forms.Form(request.POST or None);form.fields['expected_version']=forms.IntegerField(widget=forms.HiddenInput,initial=item.version);status=200
    if request.method=='POST' and form.is_valid():
        try:piece=planning.split(request.user,pk,form.cleaned_data['expected_version'],warehouse_id)
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:return redirect('stock_allocate',pk=piece.pk)
    return render(request,'projects/stock_form.html',{'form':form,'title':'Выделить 1 муфту из группового остатка','item_summary':item,'split_piece':True},status=status)


def inventory_rows(user,warehouse=None):
    result=[]
    visible_movement_ids=set(stock.movements(user).values_list('pk',flat=True)) if not permitted(user,'stock.manage') else None
    allowed_warehouses=set(stock.warehouses(user).values_list('pk',flat=True))
    allowed_jobs=set(visible_jobs(user,include_archived=True).values_list('pk',flat=True))
    for item in stock.items(user).select_related('coupling','receipt_warehouse','reserved_job__section__project_object','reserved_job__contract').prefetch_related('balances__warehouse','movements__destination','reserved_job__defects').order_by('name','method','length_mm','serial','sku'):
        balances=[b for b in item.balances.all() if b.quantity>0 and (warehouse is None or b.warehouse_id==warehouse.pk)]
        moves=[m for m in item.movements.all() if visible_movement_ids is None or m.pk in visible_movement_ids];receipts=[m for m in reversed(moves) if m.kind=='receipt']
        install=item.coupling.installation if item.coupling_id and hasattr(item.coupling,'installation') else None
        state='installed' if install else 'reserved' if item.reserved_job_id else 'free' if balances else 'catalogue'
        if warehouse and not balances and not any(m.source_id==warehouse.pk or m.destination_id==warehouse.pk for m in moves):continue
        job=install.job if install else item.reserved_job
        # Do not reveal a repair assignment to warehouse-only viewers.
        if job and job.pk not in allowed_jobs:job=None
        receipt=receipts[0] if receipts else None
        result.append({'item':item,'balances':balances,'quantity':sum(b.quantity for b in balances),'receipt':receipt,'receipt_warehouse':item.receipt_warehouse if item.receipt_warehouse_id in allowed_warehouses else (receipt.destination if receipt else None),'receipt_source':item.receipt_origin or (item.receipt_warehouse.name if item.receipt_warehouse_id in allowed_warehouses else (receipt.destination.name if receipt and receipt.destination else '')),'receipt_date':item.receipt_date if item.receipt_warehouse_id in allowed_warehouses else (receipt.date if receipt else None),'state':state,'label':{'installed':'Смонтирована','reserved':'Закреплена','free':'Свободна','catalogue':'Нет на складе'}[state],'job':job,'job_label':planning.job_label(job) if job else '', 'installed_date':install.report.performed_date if install else None,'serial':item.serial or (item.coupling.serial if item.coupling_id else '')})
    return result

def filtered_inventory(request):
    rows=inventory_rows(request.user)
    q=request.GET.get('q','').strip().casefold();wh=request.GET.get('warehouse','');kind=request.GET.get('kind','');state=request.GET.get('state','')
    if q:rows=[r for r in rows if q in ' '.join([r['item'].name,r['serial'],r['item'].method_label,r['job_label']]).casefold()]
    if wh:rows=[r for r in rows if any(str(b.warehouse_id)==wh for b in r['balances'])]
    if kind:rows=[r for r in rows if r['item'].kind==kind]
    if state:rows=[r for r in rows if r['state']==state]
    return rows

@permission_required('stock.manage')
def inventory(request):
    page=Paginator(filtered_inventory(request),50).get_page(request.GET.get('page'))
    query=request.GET.copy();query.pop('page',None)
    return render(request,'projects/stock_inventory.html',{'rows':page,'page':page,'filter_query':query.urlencode(),'warehouses':stock.warehouses(request.user).order_by('name'),'q':request.GET.get('q',''),'selected_warehouse':request.GET.get('warehouse',''),'selected_kind':request.GET.get('kind',''),'selected_state':request.GET.get('state',''),'kinds':StockItem._meta.get_field('kind').choices})

@permission_required('stock.manage')
def coverage(request):
    jobs=visible_jobs(request.user).exclude(state__in=['reported','confirmed']).exclude(contract__status='archived')
    from django.http import Http404
    try:
        if request.GET.get('contract'):jobs=jobs.filter(contract_id=uuid.UUID(request.GET['contract']))
        if request.GET.get('object'):jobs=jobs.filter(section__project_object_id=uuid.UUID(request.GET['object']))
    except ValueError:raise Http404
    demand=Counter();reserved=Counter();unknown=[]
    all_items=list(stock.items(request.user).filter(kind='coupling',archived=False).prefetch_related('balances'))
    supply=Counter()
    for item in all_items:
        if not item.reserved_job_id:
            key=(item.method,item.diameter_mm,item.length_mm,Decimal(item.wall_mm or 0))
            supply[key]+=sum(b.quantity for b in item.balances.all() if b.quantity>0)
    for job in jobs:
        lengths=planning.requirements(job)
        if not lengths: 
            if lengths is None:unknown.append({'label':planning.job_label(job),'job':job,'reason':'Уточните длину и состав конструкции (например 1+1.5).'})
            continue
        if not job.section.diameter_mm or not job.wall_mm:
            unknown.append({'label':planning.job_label(job),'job':job,'section':job.section if not job.section.diameter_mm else None,'reason':'Не указан диаметр нефтепровода или толщина стенки.'});continue
        remaining=Counter(lengths)
        for use in job.installed_couplings.select_related('coupling'):
            if use.coupling.method==job.method and use.coupling.wall_mm==job.wall_mm:remaining[use.coupling.length_mm]=max(0,remaining[use.coupling.length_mm]-1)
        for length,count in remaining.items():demand[(job.method,job.section.diameter_mm,length,job.wall_mm)]+=count
        for item in all_items:
            if item.reserved_job_id==job.pk and any(b.quantity>0 for b in item.balances.all()):reserved[(item.method,item.diameter_mm,item.length_mm,Decimal(item.wall_mm or 0))]+=1
    rows=[]
    for key,need in sorted(demand.items()):
        held=reserved[key];free=supply[key];left=max(0,need-held);missing=max(0,left-free)
        rows.append({'method':dict(METHODS)[key[0]],'diameter':key[1],'length':key[2],'wall':key[3],'need':need,'reserved':held,'free':free,'missing':missing})
    return render(request,'projects/stock_coverage.html',{'rows':rows,'unknown':unknown,'scoped':bool(request.GET.get('contract') or request.GET.get('object'))})


@permission_required('stock.manage')
def inventory_export(request):
    from django.http import HttpResponse
    from repairs.excel import workbook
    headers=['№','Наименование','Категория','Тип муфты','Диаметр, мм','Длина, мм','Стенка, мм','Заводской №','Склад получения','Дата получения','Текущий склад','Остаток','Единица','Состояние','Назначение','Дата монтажа']
    data=[]
    for n,row in enumerate(filtered_inventory(request),1):
        i=row['item'];receipt=row['receipt']
        data.append([n,i.name,i.get_kind_display(),i.method_label,i.diameter_mm or '',i.length_mm or '',i.wall_mm or '',row['serial'],row['receipt_source'],str(row['receipt_date']) if row['receipt_date'] else '', '; '.join(b.warehouse.name for b in row['balances']),row['quantity'],i.get_unit_display(),row['label'],row['job_label'],str(row['installed_date']) if row['installed_date'] else ''])
    response=HttpResponse(workbook(data,headers,sheet_name='Наличие и назначение'),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition']='attachment; filename="stock-inventory.xlsx"'
    return response

class DiameterForm(forms.Form):
    expected_diameter=forms.IntegerField(widget=forms.HiddenInput,min_value=0)
    diameter_mm=forms.IntegerField(label='Наружный диаметр нефтепровода, мм',min_value=1,max_value=10000)

@permission_required('stock.manage')
def diameter(request,pk):
    from repairs.access import can_prepare, visible_sections
    from django.core.exceptions import PermissionDenied
    from django.db import transaction
    from accounts.views import audit
    if not can_prepare(request.user):raise PermissionDenied
    section=get_object_or_404(visible_sections(request.user),pk=pk)
    form=DiameterForm(request.POST or None,initial={'expected_diameter':section.diameter_mm or 0,'diameter_mm':section.diameter_mm});status=200
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                actor=stock.manager(request.user);stock.lock()
                if not can_prepare(actor):raise PermissionDenied
                fresh=visible_sections(actor).select_for_update(of=('self',)).get(pk=pk)
                if fresh.project_object.status=='archived' or fresh.jobs.filter(contract__status='archived').exists():raise ValidationError('Участок или связанный контракт находится в архиве. Сначала восстановите его.')
                if (fresh.diameter_mm or 0)!=form.cleaned_data['expected_diameter']:raise ValidationError('Диаметр изменён другим пользователем. Откройте форму заново.')
                before=fresh.diameter_mm;fresh.diameter_mm=form.cleaned_data['diameter_mm'];fresh.full_clean();fresh.save(update_fields=['diameter_mm'])
                audit(actor,'stock_pipeline_diameter_updated',section=str(pk),previous=before,value=fresh.diameter_mm)
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:return redirect('stock_coverage')
    return render(request,'projects/stock_form.html',{'form':form,'title':'Уточнить диаметр: '+section.pipeline+' · '+section.project_object.name},status=status)
