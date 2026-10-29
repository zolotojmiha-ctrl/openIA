import uuid
from collections import Counter
from decimal import Decimal
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q,Prefetch
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render, redirect
from django.utils import timezone
from accounts.permissions import permission_required, permitted
from repairs.access import visible_jobs
from repairs.models import Job, METHODS
from .models import StockItem,StockMovement
from .stock_views import ItemForm, DATE
from . import stock, stock_planning as planning

class ReceiptForm(ItemForm):
    return_inventory=forms.BooleanField(required=False,widget=forms.HiddenInput)
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
    initial={'return_inventory':request.GET.get('return_inventory')=='1'}
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
    if request.method=='GET' and 'warehouse' not in initial:
        only=list(form.fields['warehouse'].queryset[:2])
        if len(only)==1:form.initial['warehouse']=only[0]
    if request.method=='POST' and form.is_valid():
        c=form.cleaned_data
        try:wh=planning.receive(request.user,c,c['warehouse'],c['quantity'],c['date'],c['note'],c['token'])
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:
            messages.success(request,'Поступление сохранено. Муфты добавлены поштучно в остатки.')
            return redirect('stock_inventory') if c.get('return_inventory') else redirect('stock_detail',pk=wh.pk)
    return render(request,'projects/stock_form.html',{'form':form,'title':'Добавить материал / муфты на склад','receipt':True},status=status)

class AllocationForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    job=forms.ModelChoiceField(label='Закрепить за работой / дефектами',queryset=None,required=False,empty_label='Свободна — снять закрепление')
    serial=forms.CharField(label='Заводской № (необязательно)',max_length=100,required=False)
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['job'].queryset=visible_jobs(user).exclude(state__in=['reported','confirmed','unrepairable']).exclude(contract__status='archived')
        self.fields['job'].label_from_instance=planning.job_label

@permission_required('stock.manage')
def allocate(request,pk):
    item=get_object_or_404(stock.items(request.user),pk=pk)
    if request.method=='GET':
        from .stock_groups import groups
        from django.urls import reverse
        from urllib.parse import urlencode
        group=next((g for g in groups(request.user) if any(m['item'].pk==pk for m in g['members'])),None)
        if not group or not group['warehouse'] or group['quantity']<=0:
            messages.error(request,'Сначала укажите склад и количество в режиме «Редактировать список».');return redirect('stock_inventory')
        member=next(m for m in group['members'] if m['item'].pk==pk)
        query={'group':group['token']}
        if member['quantity']==1:query['item']=str(pk)
        return redirect(reverse('stock_distribute')+'?'+urlencode(query))
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
    snapshot=forms.CharField(widget=forms.HiddenInput)
    warehouse_id=forms.UUIDField(required=False,widget=forms.HiddenInput)
    all_warehouses=forms.BooleanField(label='Подтверждаю удаление со ВСЕХ перечисленных складов',required=False)
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    note=forms.CharField(label='Причина удаления ошибочной позиции',max_length=1200,widget=forms.Textarea)

@permission_required('stock.manage')
def delete(request,pk):
    item=get_object_or_404(stock.items(request.user),pk=pk);status=200
    raw=request.POST.get('warehouse_id') if request.method=='POST' else request.GET.get('warehouse')
    warehouse=None
    if raw:
        from django.http import Http404
        try:wid=uuid.UUID(raw)
        except ValueError:raise Http404
        warehouse=get_object_or_404(stock.warehouses(request.user),pk=wid)
    balances=item.balances.filter(quantity__gt=0).select_related('warehouse')
    if warehouse:balances=balances.filter(warehouse=warehouse)
    balances=list(balances)
    form=DeleteForm(request.POST or None,initial={'expected_version':item.version,'warehouse_id':warehouse.pk if warehouse else None,'snapshot':planning.delete_snapshot(request.user,item,warehouse.pk if warehouse else None)})
    if warehouse or len(balances)<=1:form.fields.pop('all_warehouses')
    if request.method=='POST' and form.is_valid():
        try:planning.delete(request.user,pk,form.cleaned_data['expected_version'],form.cleaned_data['note'],warehouse.pk if warehouse else None,form.cleaned_data.get('all_warehouses',False),form.cleaned_data['snapshot'])
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:
            messages.success(request,'Позиция удалена с выбранного склада. Остальные склады и история сохранены.' if warehouse else 'Позиция удалена. История сохранена.')
            return redirect('stock_detail',pk=warehouse.pk) if warehouse else redirect('stock_inventory')
    return render(request,'projects/stock_form.html',{'form':form,'title':'Удалить со склада '+warehouse.name if warehouse else 'Удалить карточку со всех складов','item_summary':item,'deletion':True,'delete_balances':balances,'warehouse':warehouse},status=status)

@permission_required('stock.manage')
def split(request,pk,warehouse_id):
    item=get_object_or_404(stock.items(request.user),pk=pk);form=forms.Form(request.POST or None);form.fields['expected_version']=forms.IntegerField(widget=forms.HiddenInput,initial=item.version);status=200
    if request.method=='POST' and form.is_valid():
        try:piece=planning.split(request.user,pk,form.cleaned_data['expected_version'],warehouse_id)
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:return redirect('stock_allocate',pk=piece.pk)
    return render(request,'projects/stock_form.html',{'form':form,'title':'Выделить 1 муфту из группового остатка','item_summary':item,'split_piece':True},status=status)


def inventory_rows(user,warehouse=None,include_archived=False):
    result=[]
    visible_movement_ids=set(stock.movements(user).values_list('pk',flat=True)) if not permitted(user,'stock.manage') else None
    allowed_warehouses=set(stock.warehouses(user).values_list('pk',flat=True))
    allowed_jobs=set(visible_jobs(user,include_archived=True).values_list('pk',flat=True))
    qs=stock.items(user)
    if not include_archived:qs=qs.filter(Q(archived=False)|Q(coupling__installation__isnull=False))
    qs=qs.select_related('coupling__installation__report','coupling__installation__job__section__project_object','coupling__installation__job__contract','receipt_warehouse','reserved_job__section__project_object','reserved_job__contract').prefetch_related('balances__warehouse',Prefetch('movements',queryset=StockMovement.objects.select_related('source','destination')),'reserved_job__defects','coupling__installation__job__defects')
    for item in qs.order_by('name','method','length_mm','serial','sku'):
        balances=[b for b in item.balances.all() if b.quantity>0 and (warehouse is None or b.warehouse_id==warehouse.pk)]
        moves=[m for m in item.movements.all() if visible_movement_ids is None or m.pk in visible_movement_ids];receipts=[m for m in reversed(moves) if m.kind=='receipt']
        install=item.coupling.installation if item.coupling_id and hasattr(item.coupling,'installation') else None
        state='installed' if install else 'reserved' if item.reserved_job_id else 'free' if balances else 'catalogue'
        if warehouse and not balances and not any(m.source_id==warehouse.pk or m.destination_id==warehouse.pk for m in moves):continue
        job=install.job if install else item.reserved_job
        # Do not reveal a repair assignment to warehouse-only viewers.
        if job and job.pk not in allowed_jobs:job=None
        receipt=receipts[0] if receipts else None
        result.append({'item':item,'movements':moves,'balances':balances,'quantity':sum(b.quantity for b in balances),'receipt':receipt,'receipt_warehouse':item.receipt_warehouse if item.receipt_warehouse_id in allowed_warehouses else (receipt.destination if receipt else None),'receipt_source':item.receipt_origin or (item.receipt_warehouse.name if item.receipt_warehouse_id in allowed_warehouses else (receipt.destination.name if receipt and receipt.destination else '')),'receipt_date':item.receipt_date if item.receipt_warehouse_id in allowed_warehouses else (receipt.date if receipt else None),'state':state,'label':{'installed':'Смонтирована','reserved':'Закреплена','free':'Свободна','catalogue':'Нет на складе'}[state],'job':job,'job_label':planning.job_label(job) if job else '', 'installed_date':install.report.performed_date if install else None,'serial':item.serial or (item.coupling.serial if item.coupling_id else '')})
    return result

def filtered_inventory(request):
    from .stock_groups import groups
    rows=groups(request.user,include_archived=request.GET.get('archive')=='1')
    q=request.GET.get('q','').strip().casefold();wh=request.GET.get('warehouse','');kind=request.GET.get('kind','');state=request.GET.get('state','')
    if q:rows=[r for r in rows if q in r['search']]
    if wh:rows=[r for r in rows if r['warehouse'] and str(r['warehouse'].pk)==wh]
    if kind:rows=[r for r in rows if r['item'].kind==kind]
    if state:rows=[r for r in rows if any(m['state']==state for m in r['members'])]
    return rows

@permission_required('stock.manage')
def inventory(request):
    from . import stock_inline
    if request.method=='POST':return stock_inline.save(request)
    page=Paginator(filtered_inventory(request),50).get_page(request.GET.get('page'))
    mode=request.GET.get('mode','')
    if mode not in ('edit','delete'):mode=''
    if mode:stock_inline.prepare(request.user,page,mode)
    query=request.GET.copy();query.pop('page',None);query.pop('mode',None)
    back=request.GET.copy();back.pop('mode',None)
    from django.urls import reverse
    return render(request,'projects/stock_inventory.html',{'rows':page,'page':page,'mode':mode,'operation':uuid.uuid4(),'return_url':reverse('stock_inventory')+('?' + back.urlencode() if back else ''),'filter_query':query.urlencode(),'warehouses':stock.warehouses(request.user).order_by('name'),'q':request.GET.get('q',''),'selected_warehouse':request.GET.get('warehouse',''),'selected_kind':request.GET.get('kind',''),'selected_state':request.GET.get('state',''),'kinds':StockItem._meta.get_field('kind').choices})

@permission_required('stock.manage')
def coverage(request):
    jobs=visible_jobs(request.user).exclude(state__in=['reported','confirmed','unrepairable']).exclude(contract__status='archived')
    from django.http import Http404
    try:
        if request.GET.get('contract'):jobs=jobs.filter(contract_id=uuid.UUID(request.GET['contract']))
        if request.GET.get('object'):jobs=jobs.filter(section__project_object_id=uuid.UUID(request.GET['object']))
    except ValueError:raise Http404
    from . import stock_fit as fit
    jobs=jobs.select_related('section__project_object','contract').prefetch_related('defects','installed_couplings__coupling')
    demand=Counter();reserved=Counter();unknown=[]
    all_items=list(stock.items(request.user).filter(kind='coupling',archived=False).prefetch_related('balances'))
    supply=Counter();held_by_job={}
    for item in all_items:
        quantity=sum(b.quantity for b in item.balances.all() if b.quantity>0)
        if item.reserved_job_id:
            if quantity>0:held_by_job.setdefault(item.reserved_job_id,[]).append(item)
        else:
            key=(item.method,item.diameter_mm,item.length_mm,Decimal(item.wall_mm or 0))
            supply[key]+=quantity
    for job in jobs:
        lengths=planning.requirements(job)
        if not lengths:
            if lengths is None:unknown.append({'label':planning.job_label(job),'job':job,'reason':'Уточните длину и состав конструкции (например 1+1.5).'})
            continue
        if not job.section.diameter_mm or not job.wall_mm:
            unknown.append({'label':planning.job_label(job),'job':job,'section':job.section if not job.section.diameter_mm else None,'reason':'Не указан диаметр нефтепровода или толщина стенки.'});continue
        installations=list(job.installed_couplings.all())
        remaining=fit.consume(job,[use.coupling for use in installations],check_diameter=False)
        installed_ids={use.coupling_id for use in installations}
        held=[item for item in held_by_job.get(job.pk,[]) if item.coupling_id not in installed_ids]
        uncovered=fit.consume(job,held,remaining)
        for length,count in remaining.items():
            if not count:continue
            key=(job.method,job.section.diameter_mm,length,job.wall_mm)
            demand[key]+=count;reserved[key]+=count-uncovered[length]
    # Allocate the shared free stock once across all requirements. A larger
    # piece that fits several rows must not make every row look covered.
    available=fit.allocate(Counter({key:need-reserved[key] for key,need in demand.items()}),supply)
    rows=[]
    for key,need in sorted(demand.items()):
        held=reserved[key];free=available[key];missing=max(0,need-held-free)
        rows.append({'method':dict(METHODS)[key[0]],'diameter':key[1],'length':key[2],'wall':key[3],'need':need,'reserved':held,'free':free,'missing':missing})
    return render(request,'projects/stock_coverage.html',{'rows':rows,'unknown':unknown,'scoped':bool(request.GET.get('contract') or request.GET.get('object'))})



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
