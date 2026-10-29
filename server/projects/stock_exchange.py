"""Checked, atomic XLSX round trip for warehouse groups and repair reservations."""
import io,json,uuid,zipfile
from collections import defaultdict,Counter
from datetime import date,timedelta
from decimal import Decimal
from xml.etree import ElementTree as ET
from django import forms
from django.core import signing
from django.core.exceptions import ValidationError,ObjectDoesNotExist
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render,redirect
from django.contrib import messages
from django.utils import timezone
from accounts.permissions import permission_required
from repairs.excel import workbook
from repairs.importer import read_book,numeric,method
from repairs.access import visible_jobs
from . import stock,stock_groups as groups,stock_planning as planning
from .models import StockItem,StockImportReceipt

HEADERS={
 'Остатки':['Код строки','Версия строки (не менять)','Склад','Наименование','Категория','Единица','Тип муфты','Диаметр, мм','Длина, мм','Стенка, мм','Марка электродов','Диаметр электрода, мм','На складе (справочно)','Закреплено (справочно)','Свободно (справочно)','Смонтировано (справочно)','Новый остаток','Обновить распределение (да/нет)','Дата получения','Откуда получено','Количество нового поступления','Дата нового поступления','Откуда новая поставка'],
 'Распределение':['Код строки','Работа (скопировать с листа Работы)','Количество, шт.','Склад (справочно)'],
 'Изделия':['Код строки','ID позиции (не менять)','Версия (не менять)','Заводской №','Состояние (справочно)','Склад','Количество (справочно)','Дата получения (справочно)','Откуда получено','Назначение','Дата монтажа (справочно)'],
 'Перемещения':['Код строки','На какой склад','Количество','Дата','Перемещать резервы (да/нет)','Основание'],
 'Работы':['Работа','Контракт','Участок','Нефтепровод','Км','№ дефектов'],
}
KIND=dict(StockItem._meta.get_field('kind').choices);UNIT=dict(StockItem._meta.get_field('unit').choices)

def book(sheets):
    # Reuse the existing safe inline-string writer; compose its sheets into one workbook.
    contents=ET.Element('Types',xmlns='http://schemas.openxmlformats.org/package/2006/content-types')
    for ext,mime in [('rels','application/vnd.openxmlformats-package.relationships+xml'),('xml','application/xml')]:ET.SubElement(contents,'Default',Extension=ext,ContentType=mime)
    ET.SubElement(contents,'Override',PartName='/xl/workbook.xml',ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml')
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main';rel='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    root=ET.Element('workbook',xmlns=ns);nodes=ET.SubElement(root,'sheets');rels=ET.Element('Relationships',xmlns='http://schemas.openxmlformats.org/package/2006/relationships')
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as out:
        for n,(name,headers,rows) in enumerate(sheets,1):
            with zipfile.ZipFile(io.BytesIO(workbook(rows,headers,sheet_name=name))) as one:
                out.writestr(f'xl/worksheets/sheet{n}.xml',one.read('xl/worksheets/sheet1.xml'))
            ET.SubElement(nodes,'sheet',name=name,sheetId=str(n),attrib={'{'+rel+'}id':f'rId{n}'})
            ET.SubElement(rels,'Relationship',Id=f'rId{n}',Type=rel+'/worksheet',Target=f'worksheets/sheet{n}.xml')
            ET.SubElement(contents,'Override',PartName=f'/xl/worksheets/sheet{n}.xml',ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
        out.writestr('[Content_Types].xml',ET.tostring(contents,encoding='utf-8',xml_declaration=True));out.writestr('xl/workbook.xml',ET.tostring(root,encoding='utf-8',xml_declaration=True));out.writestr('xl/_rels/workbook.xml.rels',ET.tostring(rels,encoding='utf-8',xml_declaration=True))
        out.writestr('_rels/.rels',f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    return stream.getvalue()

def main_row(g):
    i=g['item'];dates={str(m['receipt_date'] or '') for m in g['members']};sources={m['receipt_source'] for m in g['members']}
    return [g['id'],g['token'],g['warehouse'].name if g['warehouse'] else '',i.name,KIND[i.kind],UNIT[i.unit],i.method_label,i.diameter_mm or '',i.length_mm or '',i.wall_mm or '',i.electrode_brand,str(i.electrode_diameter or ''),str(g['quantity']),str(g['reserved']),str(g['free']),str(g['installed']),str(g['quantity']),'да' if i.kind=='coupling' and g['warehouse'] and sum(d['quantity'] for d in g['distribution'] if d['state']=='reserved')==g['reserved'] else 'нет',next(iter(dates)) if len(dates)==1 else 'Разные',next(iter(sources)) if len(sources)==1 else 'Разные','','','']

def closed_label(job):return planning.job_label(job)+f' · Завершено / закрыто: только снятие · ID {job.pk}'

def work_choices(user):
    jobs=list(visible_jobs(user).exclude(state__in=['reported','confirmed']).exclude(contract__status='archived').exclude(section__project_object__status='archived'))
    labels={str(j.pk):planning.job_label(j) for j in jobs};counts=Counter(labels.values())
    return [(j,labels[str(j.pk)]+(f' · {j.get_method_display()} · ID {j.pk}' if counts[labels[str(j.pk)]]>1 else '')) for j in jobs]

@permission_required('stock.manage')
def export(request):
    from .stock_planning_views import filtered_inventory
    data=filtered_inventory(request);main=[];alloc=[];pieces=[]
    available=work_choices(request.user);work_labels={str(j.pk):label for j,label in available}
    for g in data:
        main.append(main_row(g))
        for d in g['distribution']:
            if d['state']=='reserved':alloc.append([g['id'],work_labels.get(str(d['job'].pk),closed_label(d['job'])),str(d['quantity']),g['warehouse'].name if g['warehouse'] else ''])
        for m in g['members']:
            pieces.append([g['id'],str(m['item'].pk),m['item'].version,m['serial'],m['label'],g['warehouse'].name if g['warehouse'] else '',str(m['quantity']),str(m['receipt_date'] or ''),m['receipt_source'],work_labels.get(str(m['job'].pk),closed_label(m['job'])) if m['job'] else '',str(m['installed_date'] or '')])
    if len(main)>2000 or len(pieces)>2000:return HttpResponse('Для обмена за один раз — до 2000 строк / изделий. Уточните фильтры склада и категории.',status=400)
    jobs=[[label,j.contract.number if j.contract else '',j.section.project_object.name,j.section.pipeline,', '.join(d.km for d in j.active_defects),', '.join(d.number for d in j.active_defects)] for j,label in available]
    guide=[['Остатки','Новый остаток — корректировка. Для повторной поставки заполните количество/дату/источник нового поступления или добавьте НОВАЯ-1 с датой и источником. Одинаковые типоразмеры разрешены. Изменение склада переносит всю строку. Карточке без склада можно указать склад и начальное количество.'],['Распределение','Для строк с «Обновить распределение = да» задайте полный список назначений. Пустой список снимет прежние резервы. Работа копируется с листа «Работы».'],['Изделия','Меняйте заводской номер и источник. Для нескольких номеров старой групповой позиции добавьте строки: тот же код группы, пустые ID/версия/состояние/дата/назначение, количество 1, заводской №. Общий остаток не изменится. Склад и назначение конкретного изделия можно менять; одновременно не меняйте групповое распределение этой строки.'],['Перемещения','Для частичного перемещения укажите код исходной строки, склад получателя, количество, дату и явно да/нет для перемещения резервов.'],['Защита','Не меняйте коды и версии. Срок файла не ограничен, но при изменении данных в ЛК нужна свежая версия. Импорт сначала показывает проверку; все изменения сохраняются вместе.'],['Единицы','Изоляция м²; электроды кг, с маркой и диаметром. Смонтированные изделия нельзя менять через остатки.']]
    response=HttpResponse(book([('Остатки',HEADERS['Остатки'],main),('Распределение',HEADERS['Распределение'],alloc),('Изделия',HEADERS['Изделия'],pieces),('Работы',HEADERS['Работы'],jobs),('Перемещения',HEADERS['Перемещения'],[]),('Инструкция',['Лист','Как заполнить'],guide)]),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition']='attachment; filename="stock-exchange.xlsx"'
    return response

def parse(upload):
    tables={}
    for name in ['Остатки','Распределение','Изделия','Перемещения']:
        try:sheet=read_book(upload,name)
        except ValidationError as error:
            if name=='Перемещения' and any('Не найден выбранный лист' in m for m in error.messages):tables[name]=[];continue
            raise
        cells=sheet['cells'];headers=HEADERS[name]
        if sheet['formulas']:raise ValidationError(f'Лист «{name}»: замените формулы значениями.')
        incoming=[str(cells.get((1,c),'')).strip() for c in range(1,len(headers)+1)]
        if name=='Изделия':incoming=[{'Склад (справочно)':'Склад','Назначение (справочно)':'Назначение'}.get(v,v) for v in incoming]
        legacy=headers[:20]+['']*3 if name=='Остатки' else headers
        if incoming not in [headers,legacy]:raise ValidationError(f'Лист «{name}»: столбцы не совпадают с выгруженным шаблоном.')
        rows=[]
        for n in sorted({r for r,c in cells if r>1}):
            values=[str(cells.get((n,c),'')).strip() for c in range(1,len(headers)+1)]
            if any(values):rows.append({'row':n,'values':values})
        if len(rows)>2000:raise ValidationError(f'Лист «{name}»: максимум 2000 строк за импорт.')
        tables[name]=rows
    return tables

def amount(value,whole=False):
    number=numeric(value)
    if number is None or number<0 or number>=Decimal('1000000000000000') or number.normalize().as_tuple().exponent < -3 or whole and number!=number.to_integral_value():raise ValidationError('Количество должно быть неотрицательным числом; для муфт — целым.')
    return number

def date_value(value):
    try:return date.fromisoformat(value)
    except ValueError:
        n=numeric(value)
        if n is not None and n==n.to_integral_value() and 0<n<100000:return date(1899,12,30)+timedelta(days=int(n))
        raise ValidationError('Дата получения: используйте ГГГГ-ММ-ДД.')

def label_job(user,label):
    matches=[j for j,text in work_choices(user) if text==label]
    if len(matches)!=1:raise ValidationError('Неизвестная или неоднозначная работа. Скопируйте значение с листа «Работы» свежего файла.')
    return matches[0]

def validate_existing(row,g):
    expected=[str(v) for v in main_row(g)]
    # Name is editable. Source/date in a grouped record are informational.
    for c in [0,4,5,6,7,8,9,10,11,12,13,14,15]+([18,19] if g['warehouse'] else []):
        a=row[c];b=expected[c]
        equal=numeric(a)==numeric(b) if c in [7,8,9,11,12,13,14,15] and a and b else a==b
        if not equal:raise ValidationError(f'Не меняйте справочную колонку «{HEADERS["Остатки"][c]}». Для другого склада / типоразмера добавьте новую строку.')

def fresh_group(user,key):
    return next((g for g in groups.groups(user) if g['id']==key),None)

def apply(user,tables,token=None,dry=False):
    from .stock_exchange_apply import apply as perform
    return perform(user,tables,token,dry)

class Upload(forms.Form):
    file=forms.FileField(label='Изменённый файл обмена XLSX',widget=forms.FileInput(attrs={'accept':'.xlsx'}))

@permission_required('stock.manage')
def upload(request):
    form=Upload(request.POST or None,request.FILES or None);preview='';changes=None;status=200
    if request.method=='POST':
        try:
            if request.POST.get('apply')=='1':
                payload=signing.loads(request.POST.get('preview',''),salt='stock-roundtrip',max_age=3600)
                if payload['actor']!=str(request.user.pk):raise ValidationError('Чужая проверка файла.')
                apply(request.user,payload['tables'],uuid.UUID(payload['token']));messages.success(request,'Остатки и распределение из Excel сохранены.');return redirect('stock_inventory')
            elif form.is_valid():
                tables=parse(form.cleaned_data['file']);changes=apply(request.user,tables,dry=True)
                preview=signing.dumps({'actor':str(request.user.pk),'tables':tables,'token':str(uuid.uuid4())},salt='stock-roundtrip',compress=True)
        except (ValidationError,signing.BadSignature,ValueError,KeyError,TypeError) as e:
            form.add_error(None,'; '.join(e.messages) if isinstance(e,ValidationError) else 'Проверка устарела или файл повреждён. Загрузите файл заново.');status=409
    return render(request,'projects/stock_exchange.html',{'form':form,'preview':preview,'changes':changes},status=status)
