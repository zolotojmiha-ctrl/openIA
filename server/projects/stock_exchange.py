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
 'Остатки':['Код строки','Версия строки (не менять)','Склад','Наименование','Категория','Единица','Тип муфты','Диаметр, мм','Длина, мм','Стенка, мм','Марка электродов','Диаметр электрода, мм','На складе (справочно)','Закреплено (справочно)','Свободно (справочно)','Смонтировано (справочно)','Новый остаток','Обновить распределение (да/нет)','Дата получения','Откуда получено'],
 'Распределение':['Код строки','Работа (скопировать с листа Работы)','Количество, шт.','Склад (справочно)'],
 'Изделия':['Код строки','ID позиции (не менять)','Версия (не менять)','Заводской №','Состояние (справочно)','Склад (справочно)','Количество (справочно)','Дата получения (справочно)','Откуда получено','Назначение (справочно)','Дата монтажа (справочно)'],
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
    return [g['id'],g['token'],g['warehouse'].name if g['warehouse'] else '',i.name,KIND[i.kind],UNIT[i.unit],i.method_label,i.diameter_mm or '',i.length_mm or '',i.wall_mm or '',i.electrode_brand,str(i.electrode_diameter or ''),str(g['quantity']),str(g['reserved']),str(g['free']),str(g['installed']),str(g['quantity']),'да' if i.kind=='coupling' and g['warehouse'] else 'нет',next(iter(dates)) if len(dates)==1 else 'Разные',next(iter(sources)) if len(sources)==1 else 'Разные']

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
            pieces.append([g['id'],str(m['item'].pk),m['item'].version,m['serial'],m['label'],g['warehouse'].name if g['warehouse'] else '',str(m['quantity']),str(m['receipt_date'] or ''),m['receipt_source'],m['job_label'],str(m['installed_date'] or '')])
    if len(main)>2000 or len(pieces)>2000:return HttpResponse('Для обмена за один раз — до 2000 строк / изделий. Уточните фильтры склада и категории.',status=400)
    jobs=[[label,j.contract.number if j.contract else '',j.section.project_object.name,j.section.pipeline,', '.join(d.km for d in j.active_defects),', '.join(d.number for d in j.active_defects)] for j,label in available]
    guide=[['Остатки','Меняйте «Новый остаток» и наименование. Для новой позиции: код НОВАЯ-1, склад, характеристики, новый остаток, дата получения.'],['Распределение','Для строк с «Обновить распределение = да» задайте полный список назначений. Пустой список снимет прежние резервы. Работа копируется с листа «Работы».'],['Изделия','Меняйте заводской номер и источник. Для нескольких номеров старой групповой позиции добавьте строки: тот же код группы, пустые ID/версия/состояние/дата/назначение, количество 1, заводской №. Общий остаток не изменится.'],['Защита','Не меняйте коды и версии. Скачайте свежий файл перед правкой. Импорт сначала показывает проверку; все изменения сохраняются вместе.'],['Единицы','Изоляция м²; электроды кг, с маркой и диаметром. Смонтированные изделия нельзя менять через остатки.']]
    response=HttpResponse(book([('Остатки',HEADERS['Остатки'],main),('Распределение',HEADERS['Распределение'],alloc),('Изделия',HEADERS['Изделия'],pieces),('Работы',HEADERS['Работы'],jobs),('Инструкция',['Лист','Как заполнить'],guide)]),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition']='attachment; filename="stock-exchange.xlsx"'
    return response

def parse(upload):
    tables={}
    for name in ['Остатки','Распределение','Изделия']:
        sheet=read_book(upload,name);cells=sheet['cells'];headers=HEADERS[name]
        if sheet['formulas']:raise ValidationError(f'Лист «{name}»: замените формулы значениями.')
        if [str(cells.get((1,c),'')).strip() for c in range(1,len(headers)+1)]!=headers:raise ValidationError(f'Лист «{name}»: столбцы не совпадают с выгруженным шаблоном.')
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
    for c in [0,2,4,5,6,7,8,9,10,11,12,13,14,15,18,19]:
        a=row[c];b=expected[c]
        equal=numeric(a)==numeric(b) if c in [7,8,9,11,12,13,14,15] and a and b else a==b
        if not equal:raise ValidationError(f'Не меняйте справочную колонку «{HEADERS["Остатки"][c]}». Для другого склада / типоразмера добавьте новую строку.')

def fresh_group(user,key):
    return next((g for g in groups.groups(user) if g['id']==key),None)

@transaction.atomic
def apply(user,tables,token=None,dry=False):
    actor=stock.manager(user);stock.lock()
    if token:
        old=StockImportReceipt.objects.filter(token=token).first()
        if old:
            if old.actor_id!=actor.pk:raise ValidationError('Чужой запрос импорта.')
            return []
    initial=groups.groups(actor);mapped={};changes=[];main={};main_rows={};metadata={};renames={};extra_pieces=[];seen_pieces=set()
    for entry in tables['Остатки']:
        r=entry['values'];key=r[0]
        try:
            if not key or key in main:raise ValidationError('Нужен уникальный код строки.')
            if r[17].casefold() not in ('да','нет'):raise ValidationError('Обновить распределение: укажите да или нет.')
            if r[1]:
                g=groups.current(actor,r[1],initial)
                if g['id']!=key:raise ValidationError('Код и версия относятся к разным строкам.')
                validate_existing(r,g);mapped[key]=g
                target=amount(r[16],g['item'].kind=='coupling')
                if not g['warehouse'] and target!=g['quantity']:raise ValidationError('Для карточки без склада оформите поступление в ЛК или добавьте строку НОВАЯ-1 в Excel.')
            else:
                if not key.startswith('НОВАЯ-'):raise ValidationError('Для новой позиции укажите код НОВАЯ-1, НОВАЯ-2 и т.д.')
                target=amount(r[16],r[4].casefold()=='муфта')
                if target<=0:raise ValidationError('У новой позиции нужен положительный остаток.')
                mapped[key]=None
            if len(r[3])>200 or not r[3]:raise ValidationError('Укажите наименование до 200 символов.')
            main[key]=(r,target);main_rows[key]=entry['row']
        except ValidationError as e:raise ValidationError(f'Остатки, строка {entry["row"]}: '+ '; '.join(e.messages))
    if not main:raise ValidationError('На листе «Остатки» нет строк.')
    targets=defaultdict(lambda:defaultdict(int))
    for entry in tables['Распределение']:
        r=entry['values'];key=r[0]
        try:
            if key not in main:raise ValidationError('Код строки отсутствует на листе «Остатки».')
            if main[key][0][17].casefold()!='да':raise ValidationError('Для изменения назначений включите «Обновить распределение = да».')
            q=amount(r[2],True)
            if q:
                try:job=label_job(actor,r[1])
                except ValidationError:
                    old=[d for d in (mapped[key]['distribution'] if mapped[key] else []) if d['state']=='reserved' and closed_label(d['job'])==r[1] and q<=d['quantity']]
                    if len(old)!=1:raise
                    job=old[0]['job']
                targets[key][str(job.pk)]+=int(q)
        except ValidationError as e:raise ValidationError(f'Распределение, строка {entry["row"]}: '+ '; '.join(e.messages))
    for entry in tables['Изделия']:
        r=entry['values']+['']*(len(HEADERS['Изделия'])-len(entry['values']));key=r[0]
        try:
            if key not in main:raise ValidationError('Код изделия отсутствует на листе остатков.')
            g=mapped[key]
            if not g and r[1]:raise ValidationError('У новой строки не может быть ID старого изделия.')
            if not r[1]:
                if (g and (g['item'].kind!='coupling' or not g['warehouse']) or not g and main[key][0][4].casefold()!='муфта') or amount(r[6] or '1',True)!=1 or not r[3] or len(r[3])>100 or len(r[8])>200:raise ValidationError('Для дополнительного заводского номера нужна одна муфта на складе, количество 1 и номер до 100 символов.')
                if r[2] or r[4] or r[7] or r[9] or r[10]:raise ValidationError('В новой строке изделия оставьте версию, состояние, дату и назначение пустыми.')
                extra_pieces.append((key,r[3],r[8]));continue
            member=next((m for m in g['members'] if str(m['item'].pk)==r[1]),None)
            if not member or (key,r[1]) in seen_pieces:raise ValidationError('Неизвестная или повторная позиция.')
            seen_pieces.add((key,r[1]));item=member['item']
            if str(item.version)!=r[2] or r[4]!=member['label'] or r[5]!=(g['warehouse'].name if g['warehouse'] else '') or amount(r[6])!=member['quantity'] or r[7]!=str(member['receipt_date'] or '') or r[9]!=member['job_label'] or r[10]!=str(member['installed_date'] or ''):raise ValidationError('Не меняйте справочные поля изделия; назначения меняются на листе «Распределение».')
            if len(r[3])>100 or len(r[8])>200:raise ValidationError('Слишком длинный заводской номер или источник.')
            if r[3]!=member['serial'] and item.kind!='coupling':raise ValidationError('Заводской номер заполняется для муфты.')
            if r[3] and not item.individual and not item.coupling_id and member['quantity']!=1:raise ValidationError('Сначала распределите групповую муфту по дефектам, затем скачайте отдельные изделия для заполнения номеров.')
            if r[3]!=member['serial'] or r[8]!=member['receipt_source']:
                if item.pk in metadata and metadata[item.pk]!=(r[3],r[8]):raise ValidationError('Для одной карточки указаны разные номера / источники.')
                metadata[item.pk]=(r[3],r[8])
        except ValidationError as e:raise ValidationError(f'Изделия, строка {entry["row"]}: '+ '; '.join(e.messages))
    # Every snapshot above is checked before any mutation. The company lock also
    # serializes repairs, stock movements and reservations during the apply.
    for key,(r,target) in main.items():
        g=mapped[key]
        if g:g=fresh_group(actor,g['id']) or g
        try:
            if g is None:
                whs=list(stock.warehouses(actor).filter(name=r[2],archived=False))
                if len(whs)!=1:raise ValidationError('Склад не найден или название неоднозначно. Используйте название из ЛК.')
                wh=whs[0];kind=next((k for k,v in KIND.items() if v.casefold()==r[4].casefold()),None);unit=next((k for k,v in UNIT.items() if v.casefold()==r[5].casefold()),None)
                unit=unit or {'шт':'pcs','pcs':'pcs','м2':'m2','m2':'m2','кв.м':'m2','кв. м':'m2','kg':'kg'}.get(r[5].casefold())
                if not kind or not unit:raise ValidationError('Неизвестная категория или единица.')
                def dimension(value):
                    if not value:return None
                    n=numeric(value)
                    if n is None or n<=0 or n!=n.to_integral_value() or n>100000:raise ValidationError('Размер должен быть положительным целым числом в мм.')
                    return int(n)
                data={'sku':'','name':r[3],'kind':kind,'unit':unit,'method':method(r[6]) or '','diameter_mm':dimension(r[7]),'length_mm':dimension(r[8]),'wall_mm':dimension(r[9]),'electrode_brand':r[10],'electrode_diameter':numeric(r[11]) if r[11] else None,'archived':False,'serial':'','receipt_origin':r[19]}
                probe=StockItem(**{k:v for k,v in data.items() if k not in ['sku','serial','receipt_origin','archived']})
                group_id=groups.identity(probe,wh.pk)
                if any(x['id']==group_id for x in groups.groups(actor)):raise ValidationError('Этот материал на складе уже есть. Измените «Новый остаток» существующей строки.')
                planning.receive(actor,data,wh,target,date_value(r[18]),'Поступление из Excel',uuid.uuid4())
                g=fresh_group(actor,group_id);mapped[key]=g;changes.append(f'{key}: поступление {target} {r[5]} на {wh.name}')
            else:
                delta=target-g['quantity']
                # Release only explicitly replaced reserves before a decrease.
                if r[17].casefold()=='да' and g['item'].kind=='coupling' and delta<0:
                    groups.distribute(actor,g['token'],targets[key]);g=fresh_group(actor,g['id'])
                if delta:
                    groups.correct_quantity(actor,g['token'],target);changes.append(f'{r[3]} · {r[2]}: остаток {g["quantity"]} → {target}');g=fresh_group(actor,g['id']) or {**g,'quantity':Decimal(0),'members':[]}
                mapped[key]=g
            if r[17].casefold()=='да':
                if g['item'].kind!='coupling':raise ValidationError('Распределение по дефектам поддерживается для муфт.')
                if target or targets[key]:groups.distribute(actor,g['token'],targets[key])
                changes.append(f'{r[3]} · {r[2]}: закрепить {sum(targets[key].values())} шт.; остальные свободны')
            if r[3]!=g['item'].name:
                for member in g['members']:
                    pk=member['item'].pk
                    if pk in renames and renames[pk]!=r[3]:raise ValidationError('Одна карточка материала имеет разные новые наименования.')
                    renames[pk]=r[3]
                changes.append(f'Наименование → {r[3]}')
        except (ValidationError,ObjectDoesNotExist) as e:raise ValidationError(f'Остатки, строка {main_rows[key]} ({r[3]} · {r[2]}): '+('; '.join(e.messages) if isinstance(e,ValidationError) else 'Запись недоступна.'))
    for pk,name in renames.items():
        item=stock.items(actor).get(pk=pk);data=groups.values(item);data.update(name=name,coupling=item.coupling)
        stock.save_item(actor,item.pk,item.version,data)
    for pk,(serial,origin) in metadata.items():
        item=stock.items(actor).get(pk=pk);data=groups.values(item);data.update(serial=serial,receipt_origin=origin,coupling=item.coupling)
        stock.save_item(actor,item.pk,item.version,data);changes.append(f'Номер / источник: {item.name} → {serial or "—"}')
    used_extra=set()
    for key,serial,origin in extra_pieces:
        g=mapped[key];wh=g['warehouse'];latest=fresh_group(actor,g['id'])
        if not latest:raise ValidationError('Не найден остаток для заводского номера.')
        candidates=[m for m in latest['members'] if m['quantity']>0 and not m['serial'] and m['item'].pk not in used_extra and m['state']!='installed']
        if not candidates:raise ValidationError('Для новых заводских номеров не хватает изделий без номера.')
        m=candidates[0];item=stock.items(actor).get(pk=m['item'].pk)
        if not item.individual and not item.coupling_id and (m['quantity']>1 or item.balances.filter(quantity__gt=0).count()>1):item=planning.split(actor,item.pk,item.version,wh.pk)
        data=groups.values(item);data.update(serial=serial,receipt_origin=origin or item.receipt_origin,coupling=item.coupling)
        stock.save_item(actor,item.pk,item.version,data);used_extra.add(item.pk);changes.append(f'Заводской № {serial} · {wh.name}')
    if token:StockImportReceipt.objects.create(token=token,actor=actor)
    if dry:transaction.set_rollback(True)
    return changes

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
