import uuid
from datetime import date
from django import forms
from django.core import signing
from django.core.exceptions import ValidationError,ObjectDoesNotExist
from django.db import transaction
from django.http import Http404,HttpResponse
from django.shortcuts import render,redirect
from accounts.permissions import permission_required
from . import stock
from .models import StockBalance,StockImportReceipt,StockMovement
from .access import visible_objects
from .crews import visible_assignments
from repairs.access import visible_jobs
from repairs.models import Coupling
from repairs.excel import workbook
from repairs.importer import read_book

HEADERS={
 'warehouses':['ID склада','Название','Местоположение','ID объекта','ID назначения бригады','Архив (0/1)','Версия'],
 'items':['ID позиции','Код','Наименование','Вид (material/coupling)','Ед. (pcs/m/kg/l)','Метод (p1/p2/p3/p4/p5u)','Диаметр мм','Длина мм','Стенка мм','Архив (0/1)','ID конкретной муфты','Версия'],
 'movements':['ID операции','Вид (receipt/transfer/issue/return/consume/loss)','ID позиции','Количество','ID откуда','ID куда','Дата','Основание','ID работы'],
 'balances':['ID склада','Склад','ID позиции','Код','Наименование','Количество','Ед.'],
}
def kind_ok(kind):
    if kind not in HEADERS:raise Http404

def export_rows(user,kind):
    if kind=='warehouses':
        objs=set(visible_objects(user).values_list('pk',flat=True));crews=set(visible_assignments(user).values_list('pk',flat=True))
        return [[str(w.pk),w.name,w.location,str(w.project_object_id) if w.project_object_id in objs else '',str(w.brigade_assignment_id) if w.brigade_assignment_id in crews else '',int(w.archived),w.version] for w in stock.warehouses(user).order_by('name')]
    if kind=='items':
        allowed_couplings=set(Coupling.objects.filter(project_object__in=visible_objects(user)).values_list('pk',flat=True))
        return [[str(i.pk),*[getattr(i,k) if getattr(i,k) is not None else '' for k in stock.ITEM_FIELDS[:-1]],int(i.archived),str(i.coupling_id) if i.coupling_id in allowed_couplings else '',i.version] for i in stock.items(user).order_by('sku')]
    if kind=='balances':return [[str(b.warehouse_id),b.warehouse.name,str(b.item_id),b.item.sku,b.item.name,str(b.quantity),b.item.get_unit_display()] for b in StockBalance.objects.filter(warehouse__in=stock.warehouses(user),quantity__gt=0).select_related('warehouse','item').order_by('warehouse__name','item__sku')]
    allowed=set(visible_jobs(user).values_list('pk',flat=True))
    return [[str(m.pk),m.kind,str(m.item_id),str(m.quantity),str(m.source_id or ''),str(m.destination_id or ''),str(m.date),m.note,str(m.job_id) if m.job_id in allowed else ''] for m in stock.movements(user).order_by('created_at','pk')]

@permission_required('stock.manage')
def export(request,kind):
    kind_ok(kind);result=HttpResponse(workbook([] if request.GET.get('template')=='1' else export_rows(request.user,kind),headers=HEADERS[kind]),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');result['Content-Disposition']=f'attachment; filename="armada-stock-{kind}.xlsx"';result['Cache-Control']='private, no-store';return result

def flag(raw):
    if raw not in ('0','1'):raise ValidationError('Флаг: 0 или 1.')
    return raw=='1'
def integer(raw):return int(raw) if raw else None

def apply_rows(user,kind,rows):
    actor=stock.manager(user);stock.lock()
    for row in rows:
        v=row['values']
        try:
            if kind=='warehouses':
                obj=visible_objects(actor).get(pk=uuid.UUID(v[3])) if v[3] else None;crew=visible_assignments(actor).get(pk=uuid.UUID(v[4])) if v[4] else None
                if v[0]:
                    existing=stock.warehouses(actor).get(pk=uuid.UUID(v[0]))
                    # A redacted export must not remove a hidden relationship.
                    if existing.project_object_id and not v[3] and not visible_objects(actor).filter(pk=existing.project_object_id).exists():obj=existing.project_object
                    if existing.brigade_assignment_id and not v[4] and not visible_assignments(actor).filter(pk=existing.brigade_assignment_id).exists():raise ValidationError('Для изменения этого склада нужен доступ к назначению бригады.')
                stock.save_warehouse(actor,uuid.UUID(v[0]) if v[0] else None,int(v[6]) if v[0] else None,{'name':v[1],'location':v[2],'project_object':obj,'brigade_assignment':crew,'archived':flag(v[5])})
            elif kind=='items':
                coupling=Coupling.objects.filter(project_object__in=visible_objects(actor)).get(pk=uuid.UUID(v[10])) if v[10] else None
                if v[0]:
                    existing=stock.items(actor).get(pk=uuid.UUID(v[0]))
                    if existing.coupling_id and not v[10]:raise ValidationError('Не удаляйте связь с конкретной муфтой; для правки нужен доступ к её объекту.')
                stock.save_item(actor,uuid.UUID(v[0]) if v[0] else None,int(v[11]) if v[0] else None,dict(sku=v[1],name=v[2],kind=v[3],unit=v[4],method=v[5],diameter_mm=integer(v[6]),length_mm=integer(v[7]),wall_mm=integer(v[8]),archived=flag(v[9]),coupling=coupling))
            elif kind=='movements':
                if v[0]:
                    existing=stock.movements(actor).get(pk=uuid.UUID(v[0]));expected=export_rows_for_movement(actor,existing)
                    if v!=expected:raise ValidationError('Проведённая операция не переписывается. Отмените её и добавьте новую строку с пустым ID.')
                else:stock.move(actor,uuid.UUID(v[2]),v[1],v[3],uuid.UUID(v[4]) if v[4] else None,uuid.UUID(v[5]) if v[5] else None,date.fromisoformat(v[6]),v[7],uuid.UUID(row['token']),uuid.UUID(v[8]) if v[8] else None)
            else:raise ValidationError('Остатки не переписываются. Внесите начальное поступление или корректирующие операции.')
        except (ValidationError,ValueError,TypeError,ObjectDoesNotExist) as e:
            raise ValidationError(f'Строка {row["row"]}: '+('; '.join(e.messages) if isinstance(e,ValidationError) else 'Проверьте ID, даты и значения.')) from e

def export_rows_for_movement(user,m):
    allowed=visible_jobs(user).filter(pk=m.job_id).exists() if m.job_id else False
    return [str(m.pk),m.kind,str(m.item_id),str(m.quantity),str(m.source_id or ''),str(m.destination_id or ''),str(m.date),m.note,str(m.job_id) if allowed else '']

class UploadForm(forms.Form):file=forms.FileField(label='Файл XLSX',widget=forms.FileInput(attrs={'accept':'.xlsx'}))
def parse_rows(file,kind,user):
    book=read_book(file);cells=book['cells'];headers=HEADERS[kind]
    if book['formulas']:raise ValidationError('Формулы не поддерживаются. Вставьте значения.')
    if [str(cells.get((1,i+1),'')).strip() for i in range(len(headers))]!=headers:raise ValidationError('Столбцы не совпадают с шаблоном.')
    rows=[];seen=set()
    for n in sorted({r for r,c in cells if r>1}):
        v=[str(cells.get((n,i+1),'')).strip() for i in range(len(headers))]
        if not any(v):continue
        if len(rows)>=500:raise ValidationError('За один импорт — до 500 строк.')
        if v[0] and v[0] in seen:raise ValidationError(f'Строка {n}: повтор ID.')
        if v[0]:seen.add(v[0])
        rows.append({'row':n,'values':v,'token':str(uuid.uuid4())})
    if not rows:raise ValidationError('Нет строк для переноса.')
    with transaction.atomic():apply_rows(user,kind,rows);transaction.set_rollback(True)
    return rows

@permission_required('stock.manage')
def upload(request,kind):
    kind_ok(kind)
    if kind=='balances':raise Http404
    form=UploadForm(request.POST or None,request.FILES or None);rows=None;preview='';status=200
    if request.method=='POST':
        if request.POST.get('apply')=='1':
            try:
                payload=signing.loads(request.POST.get('preview',''),salt='stock-excel',max_age=3600)
                if payload['actor']!=str(request.user.pk) or payload['kind']!=kind:raise ValidationError('Чужой предварительный просмотр.')
                with transaction.atomic():
                    actor=stock.manager(request.user);stock.lock()
                    receipt=StockImportReceipt.objects.filter(token=payload['token']).first()
                    if not receipt:
                        apply_rows(actor,kind,payload['rows']);StockImportReceipt.objects.create(token=payload['token'],actor=actor)
                    elif receipt.actor_id!=actor.pk:raise ValidationError('Чужой импорт.')
            except (signing.BadSignature,ValidationError,ValueError,KeyError,TypeError) as e:form.add_error(None,'; '.join(e.messages) if isinstance(e,ValidationError) else 'Просмотр устарел или некорректен.');status=409
            else:return redirect('stock')
        elif form.is_valid():
            try:
                rows=parse_rows(form.cleaned_data['file'],kind,request.user);preview=signing.dumps({'actor':str(request.user.pk),'kind':kind,'rows':rows,'token':str(uuid.uuid4())},salt='stock-excel',compress=True)
            except ValidationError as e:form.add_error('file',e)
    return render(request,'projects/stock_excel.html',{'form':form,'rows':rows,'preview':preview,'kind':kind,'headers':HEADERS[kind]},status=status)
