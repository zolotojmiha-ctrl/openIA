"""One checked transaction for edits made directly in the stock table."""
import hashlib,json,uuid
from decimal import Decimal
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError,ObjectDoesNotExist
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from accounts.views import audit
from repairs.models import METHODS
from .models import StockItem,StockActionReceipt
from . import stock,stock_groups as groups,stock_planning as planning,stock_exchange as exchange

SPEC=('kind','unit','method','diameter_mm','length_mm','wall_mm','electrode_brand','electrode_diameter')

class RowForm(forms.Form):
    name=forms.CharField(label='Наименование',max_length=200)
    kind=forms.ChoiceField(label='Категория',choices=StockItem._meta.get_field('kind').choices)
    unit=forms.ChoiceField(label='Единица',choices=StockItem._meta.get_field('unit').choices)
    method=forms.ChoiceField(label='Тип муфты',choices=[('','—'),*METHODS[:5]],required=False)
    diameter_mm=forms.IntegerField(label='Диаметр, мм',min_value=1,max_value=100000,required=False)
    length_mm=forms.IntegerField(label='Длина, мм',min_value=1,max_value=100000,required=False)
    wall_mm=forms.IntegerField(label='Стенка, мм',min_value=1,max_value=100000,required=False)
    electrode_brand=forms.CharField(label='Марка электродов',max_length=100,required=False)
    electrode_diameter=forms.DecimalField(label='Диаметр электрода, мм',min_value=Decimal('.01'),max_digits=6,decimal_places=2,required=False)
    warehouse=forms.ModelChoiceField(label='Склад',queryset=None,required=False,empty_label='Не указан')
    quantity=forms.DecimalField(label='На складе',min_value=0,max_digits=18,decimal_places=3)
    receipt_date=forms.DateField(label='Дата получения',required=False,widget=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}))
    receipt_origin=forms.CharField(label='Откуда получено',max_length=200,required=False)

    def __init__(self,*args,user,group,**kwargs):
        self.group=group
        active=[m for m in group['members'] if m['state']!='installed']
        dates={m['receipt_date'] for m in active};sources={m['receipt_source'] for m in active}
        i=group['item'];initial={k:getattr(i,k) for k in ('name',*SPEC)}
        initial.update(warehouse=group['warehouse'].pk if group['warehouse'] else None,quantity=group['quantity'],receipt_date=next(iter(dates)) if len(dates)==1 else None,receipt_origin=next(iter(sources)) if len(sources)==1 else '')
        super().__init__(*args,initial=initial,**kwargs)
        self.fields['warehouse'].queryset=stock.warehouses(user).order_by('name')
        self.mutable_spec=bool(active) and all(not m['movements'] and not m['item'].coupling_id for m in group['members'])
        for name,field in self.fields.items():
            field.widget.attrs.update({'data-grid-field':name,'aria-label':field.label})
            if not active or name in SPEC and not self.mutable_spec:field.disabled=True
        self.fields['receipt_date'].widget.attrs['placeholder']='Разные даты' if len(dates)>1 else ''
        self.fields['receipt_origin'].widget.attrs['placeholder']='Разные источники' if len(sources)>1 else ''

    def clean(self):
        c=super().clean();g=self.group;wh=c.get('warehouse');q=c.get('quantity')
        if q is not None:
            if c.get('kind')=='coupling' and q!=q.to_integral_value():self.add_error('quantity','Муфты учитываются целыми изделиями.')
            if q>0 and not wh:self.add_error('warehouse','Укажите склад для этого количества.')
            if wh and not g['warehouse'] and q==0:self.add_error('quantity','Укажите фактическое количество для выбранного склада.')
        if wh and wh.archived and (not g['warehouse'] or wh.pk!=g['warehouse'].pk):self.add_error('warehouse','Выберите действующий склад.')
        if c.get('receipt_date') and c['receipt_date']>timezone.localdate():self.add_error('receipt_date','Дата получения не может быть в будущем.')
        if 'receipt_date' in self.changed_data and not c.get('receipt_date'):self.add_error('receipt_date','Укажите дату получения.')
        return c

def prepare(user,rows,mode):
    for n,g in enumerate(rows):
        g['editor']=RowForm(user=user,group=g,prefix=f'row-{n}') if mode=='edit' else None
        g['serial_members']=[m for m in g['members'] if m['item'].kind=='coupling' and m['state']!='installed' and (m['item'].individual or m['item'].coupling_id or m['quantity']<=1)]
        g['can_delete']=any(m['state']!='installed' for m in g['members'])

class RowError(ValidationError):
    def __init__(self,group,message):
        self.group=group
        super().__init__(message)

def values(data):return {k:('' if v is None else str(v)) for k,v in data.items()}

@transaction.atomic
def apply(user,payload):
    actor=stock.manager(user);stock.lock()
    if not isinstance(payload,dict) or payload.get('mode') not in ('edit','delete'):raise ValidationError('Неизвестный режим списка.')
    try:operation=uuid.UUID(payload.get('operation',''))
    except (ValueError,TypeError,AttributeError):raise ValidationError('Обновите страницу перед сохранением.')
    rows=payload.get('rows')
    if not isinstance(rows,list) or not rows or len(rows)>50:raise ValidationError('Выберите от 1 до 50 строк этой страницы.')
    digest=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    prior=StockActionReceipt.objects.filter(token=operation).first()
    if prior:
        if prior.actor_id!=actor.pk or prior.digest!=digest:raise ValidationError('Этот запрос уже сохранён с другими изменениями. Обновите страницу.')
        return len(rows)
    known=groups.groups(actor,include_archived=True);plans=[];seen=set()
    for r in rows:
        if not isinstance(r,dict):raise ValidationError('Повреждена строка списка.')
        g=groups.current(actor,r.get('group',''),known)
        if g['id'] in seen:raise RowError(g['id'],'Строка повторяется.')
        seen.add(g['id'])
        if payload['mode']=='delete':plans.append((g,None,{}));continue
        raw=r.get('values');serials=r.get('serials',{})
        if not isinstance(raw,dict) or not isinstance(serials,dict):raise RowError(g['id'],'Повреждены поля строки.')
        form=RowForm(raw,user=actor,group=g)
        if not form.is_valid():raise RowError(g['id'],'; '.join(f'{form.fields[k].label if k in form.fields else "Строка"}: {"; ".join(v)}' for k,v in form.errors.items()))
        eligible={str(m['item'].pk) for m in g['members'] if m['item'].kind=='coupling' and m['state']!='installed' and (m['item'].individual or m['item'].coupling_id or m['quantity']<=1)}
        if set(serials)-eligible or any(not isinstance(v,str) or len(v)>100 for v in serials.values()):raise RowError(g['id'],'Неизвестная муфта или слишком длинный заводской номер.')
        plans.append((g,form,serials))
    if payload['mode']=='delete':
        if payload.get('confirmed') is not True:raise ValidationError('Подтвердите удаление выбранных позиций.')
        for g,_,_ in plans:
            try:
                members=[m for m in g['members'] if m['state']!='installed']
                if not members:raise ValidationError('В строке осталась только история монтажа. Она сохраняется.')
                for m in members:
                    i=stock.items(actor).get(pk=m['item'].pk)
                    planning.delete(actor,i.pk,i.version,'Удаление выбранной позиции из общего списка',g['warehouse'].pk if g['warehouse'] and m['quantity']>0 else None)
            except (ValidationError,ObjectDoesNotExist) as e:raise RowError(g['id'],'; '.join(e.messages) if isinstance(e,ValidationError) else 'Позиция уже изменена.')
    else:
        table={'Остатки':[],'Распределение':[],'Изделия':[],'Перемещения':[]};meta={};serial_updates={}
        # Validate all original versions before any change. Metadata of a shared
        # card must agree when both of its warehouse rows are edited together.
        for g,f,serials in plans:
            c=f.cleaned_data
            for m in g['members']:
                if m['state']=='installed':continue
                pk=m['item'].pk;changes={}
                for k in ('receipt_date','receipt_origin'):
                    if k in f.changed_data:changes[k]=c[k]
                if pk in meta and any(k in meta[pk] and meta[pk][k]!=v for k,v in changes.items()):raise RowError(g['id'],'Для одной позиции указаны разные даты или источники получения.')
                meta.setdefault(pk,{}).update(changes)
            for pk,serial in serials.items():
                if pk in serial_updates and serial_updates[pk]!=serial:raise RowError(g['id'],'Для одной муфты указаны разные заводские номера.')
                serial_updates[pk]=serial.strip()
        # Characteristics without movement history may be corrected in place.
        for g,f,_ in plans:
            if any(k in f.changed_data for k in SPEC):
                for m in g['members']:
                    i=stock.items(actor).get(pk=m['item'].pk);data=groups.values(i);data.update({k:f.cleaned_data[k] for k in SPEC});data['coupling']=i.coupling
                    try:stock.save_item(actor,i.pk,i.version,data)
                    except ValidationError as e:raise RowError(g['id'],e.messages)
        refreshed=groups.groups(actor,include_archived=True)
        for n,(old,f,_) in enumerate(plans,2):
            i=stock.items(actor).get(pk=old['item'].pk);key=groups.identity(i,old['warehouse'].pk if old['warehouse'] else None)
            g=next(x for x in refreshed if x['id']==key);c=f.cleaned_data;r=[str(v) for v in exchange.main_row(g)]
            r[3]=c['name'];r[2]=c['warehouse'].name if c['warehouse'] else '';r[16]=str(c['quantity']);r[17]='нет'
            if not g['warehouse']:
                r[18]=str(c['receipt_date'] or timezone.localdate());r[19]=c['receipt_origin']
            table['Остатки'].append({'row':n,'values':r})
        before_ids=set(stock.items(actor).values_list('pk',flat=True))
        exchange.apply(actor,table,source_label='общего списка')
        after=groups.groups(actor,include_archived=True)
        for old,f,_ in plans:
            changes={k:f.cleaned_data[k] for k in ('receipt_date','receipt_origin') if k in f.changed_data}
            if not changes:continue
            i=stock.items(actor).get(pk=old['item'].pk);wh=f.cleaned_data['warehouse'];key=groups.identity(i,wh.pk if wh else None)
            for g in after:
                if g['id']!=key:continue
                for m in g['members']:
                    pk=m['item'].pk
                    if pk in before_ids:continue
                    if pk in meta and any(k in meta[pk] and meta[pk][k]!=v for k,v in changes.items()):raise RowError(old['id'],'Для новой позиции указаны разные даты или источники.')
                    meta.setdefault(pk,{}).update(changes)
        # Release changed numbers first so swapping two factory numbers is safe.
        for pk,new in serial_updates.items():
            i=stock.items(actor).get(pk=pk)
            if i.serial!=new and i.serial:
                data=groups.values(i);data.update(serial='',coupling=i.coupling);stock.save_item(actor,i.pk,i.version,data)
        for pk,new in serial_updates.items():
            i=stock.items(actor).get(pk=pk)
            if i.serial!=new:
                data=groups.values(i);data.update(serial=new,coupling=i.coupling);stock.save_item(actor,i.pk,i.version,data)
        for pk,changes in meta.items():
            if not changes:continue
            i=stock.items(actor).get(pk=pk)
            if 'receipt_origin' in changes:
                data=groups.values(i);data.update(receipt_origin=changes['receipt_origin'],coupling=i.coupling);i=stock.save_item(actor,i.pk,i.version,data)
            if 'receipt_date' in changes:
                if not i.receipt_warehouse_id:
                    first=i.movements.filter(kind='receipt').order_by('created_at','pk').first()
                    if first:i.receipt_warehouse_id=first.destination_id
                i.receipt_date=changes['receipt_date'];i.version+=1;i.save(update_fields=['receipt_date','receipt_warehouse','version'])
                audit(actor,'stock_receipt_date_corrected',item=str(i.pk),date=str(i.receipt_date))
    StockActionReceipt.objects.create(token=operation,actor=actor,digest=digest)
    return len(rows)

def save(request):
    try:
        raw=request.POST.get('payload','')
        if len(raw)>1500000:raise ValidationError('Слишком много изменений. Сохраните часть строк.')
        payload=json.loads(raw);count=apply(request.user,payload)
    except (ValidationError,ValueError,TypeError,ObjectDoesNotExist) as e:
        return JsonResponse({'ok':False,'group':getattr(e,'group',''),'error':'; '.join(e.messages) if isinstance(e,ValidationError) else 'Не удалось проверить изменения. Обновите список.'},status=409)
    messages.success(request,'Выбранные позиции удалены. История сохранена.' if payload['mode']=='delete' else 'Изменения списка сохранены.')
    return JsonResponse({'ok':True,'count':count})
