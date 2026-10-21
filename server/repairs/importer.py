"""Read an XLSX worksheet as evidence. Never execute formulas or external links."""
import hashlib
import io
import posixpath
import re
import zipfile
from datetime import date,timedelta
from decimal import Decimal,InvalidOperation
from xml.etree import ElementTree as ET
from django.core.exceptions import ValidationError
from .values import kilometre,identifier

NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'

def col_number(label):
    n=0
    for c in label:n=n*26+ord(c)-64
    return n

def method(value):
    text=str(value).strip().lower().replace(' ','').replace('p','п')
    if 'шлиф' in text:return 'grind'
    if 'вырез' in text:return 'cut'
    return {'п1':'p1','п2':'p2','п3':'p3','п4':'p4','п5у':'p5u'}.get(text)

def numeric(value):
    try:
        text=str(value).strip().replace(',','.')
        if len(text)>100:return None
        d=Decimal(text)
        return d if d.is_finite() and -100<=d.adjusted()<=100 else None
    except InvalidOperation:return None

def read_book(upload,sheet_name=''):
    data=upload.read(10*1024*1024+1);upload.seek(0)
    if not data or len(data)>10*1024*1024:raise ValidationError('Нужен файл XLSX размером до 10 МБ.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if len(z.infolist())>500 or sum(i.file_size for i in z.infolist())>50*1024*1024:raise ValidationError('Слишком большой состав XLSX.')
            def xml(name):
                if z.getinfo(name).file_size>10*1024*1024:raise ValidationError('Слишком большой XML-раздел XLSX.')
                raw=z.read(name)
                if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:raise ValidationError('Недопустимая структура XML.')
                return ET.fromstring(raw)
            strings=[]
            if 'xl/sharedStrings.xml' in z.namelist():strings=[''.join(t.text or '' for t in item.findall('.//s:t',NS)) for item in xml('xl/sharedStrings.xml').findall('s:si',NS)]
            formats=[]
            if 'xl/styles.xml' in z.namelist():
                styles=xml('xl/styles.xml');custom={n.get('numFmtId'):n.get('formatCode','') for n in styles.findall('s:numFmts/s:numFmt',NS)}
                formats=[custom.get(x.get('numFmtId'),'') for x in styles.findall('s:cellXfs/s:xf',NS)]
            book=xml('xl/workbook.xml');rels={r.get('Id'):r for r in xml('xl/_rels/workbook.xml.rels')}
            sheets=book.findall('s:sheets/s:sheet',NS)
            chosen=next((s for s in sheets if s.get('name')==sheet_name),None) if sheet_name else sheets[-1]
            if chosen is None:raise ValidationError('Не найден выбранный лист. Проверьте его название.')
            relationship=rels[chosen.get('{'+REL+'}id')]
            if relationship.get('TargetMode')=='External':raise ValidationError('Внешний лист не поддерживается.')
            target=relationship.get('Target','');path=target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/'+target)
            if not path.startswith('xl/worksheets/'):raise ValidationError('Недопустимый путь листа.')
            sheet=xml(path);cells={};formulas={}
            for c in sheet.findall('s:sheetData/s:row/s:c',NS):
                ref=c.get('r','');m=re.fullmatch(r'([A-Z]{1,3})(\d{1,5})',ref)
                if not m or int(m[2])>10000 or col_number(m[1])>200:raise ValidationError('Лист превышает допустимый размер.')
                raw=c.find('s:v',NS);value=raw.text if raw is not None else ''
                if c.get('t')=='s':value=strings[int(value)]
                elif c.get('t')=='inlineStr':value=''.join(t.text or '' for t in c.findall('.//s:t',NS))
                elif c.get('t') not in ('str','b','e','d') and value:
                    d=numeric(value)
                    if d is not None and d==d.to_integral_value():value=format(d,'f').split('.')[0]
                if c.get('s') is not None and c.get('t') not in ('s','inlineStr','str','b','e','d'):
                    fmt=formats[int(c.get('s'))] if int(c.get('s'))<len(formats) else ''
                    if re.fullmatch(r'0{2,20}',fmt) and value and re.fullmatch(r'\d+',value):value=value.zfill(len(fmt))
                cells[(int(m[2]),col_number(m[1]))]=value or ''
                formula=c.find('s:f',NS)
                if formula is not None:formulas[ref]=formula.text or ''
            merges=[]
            for merge in sheet.findall('s:mergeCells/s:mergeCell',NS):
                m=re.fullmatch(r'([A-Z]+)(\d+):([A-Z]+)(\d+)',merge.get('ref',''))
                if m:merges.append((int(m[2]),col_number(m[1]),int(m[4]),col_number(m[3])))
            epoch=date(1904,1,1) if book.find('s:workbookPr',NS) is not None and book.find('s:workbookPr',NS).get('date1904') in ('1','true') else date(1899,12,30)
            return {'cells':cells,'merges':merges,'formulas':formulas,'sha256':hashlib.sha256(data).hexdigest(),'sheet':chosen.get('name'),'epoch':epoch,'external_formulas':sum('[' in s for s in formulas.values())}
    except (zipfile.BadZipFile,KeyError,IndexError,ValueError,ET.ParseError,OSError,OverflowError) as e:raise ValidationError('Не удалось прочитать XLSX. Нужна обычная книга Excel без пароля.') from e

def parse_work(upload,sheet_name=''):
    book=read_book(upload,sheet_name);cells=book['cells'];merges=book['merges']
    if any(r==1 and str(v).strip()=='Код ремонта' for (r,c),v in cells.items()):
        from .excel import parse_standard
        return parse_standard(book)
    def origin(r,c):
        for r1,c1,r2,c2 in merges:
            if r1<=r<=r2 and c1<=c<=c2:return r1,c1
        return r,c
    def value(r,c,inherit=False):return cells.get(origin(r,c) if inherit else (r,c),'')
    header=None
    for r in range(1,11):
        found={c:str(v).strip().lower() for (rr,c),v in cells.items() if rr==r}
        if any('дефект' in v for v in found.values()) and any(v=='км' for v in found.values()):header=(r,found);break
    if not header:raise ValidationError('На листе не найдены заголовки «КМ» и «№ дефекта». Выберите лист с ведомостью.')
    hr,headers=header
    def find(text,exact=False):return next((c for c,v in sorted(headers.items()) if (v==text if exact else text in v)),None)
    kc=find('км',True);dc=find('дефект');sc=find('секци');mc=find('муфта',True)
    if not mc or mc>15:raise ValidationError('Не найден столбец назначенного способа ремонта.')
    lc=mc+1;cc=mc+2;pc=next((c for c,v in headers.items() if 'дата' in v and 'план' in v and c<20),None)
    note_cols=[c for c,v in headers.items() if ('примечан' in v or 'перенос' in v) and c<26]
    groups={};warnings=[];seen=set()
    if book['external_formulas']:warnings.append('Есть формулы со ссылками на другие книги. Использованы сохранённые значения Excel, формулы не пересчитывались.')
    for r in range(hr+1,max(rr for rr,c in cells)+1):
        km=numeric(value(r,kc,True));number=str(value(r,dc)).strip();m=method(value(r,mc,True))
        if km is None or not 0<=km<1000000 or not number or not m:continue
        try:
            km=kilometre(km);number=identifier(number,'№ дефекта')
            pipe=identifier(str(value(r,sc,True)).strip() if sc else '','№ секции',optional=True)
        except ValidationError as e:raise ValidationError(f'Строка {r}: '+e.messages[0])
        identity=(km,number)
        if identity in seen:raise ValidationError(f'Строка {r}: после округления повторяется дефект № {number} на км {km}.')
        seen.add(identity)
        length=numeric(value(r,lc,True));comp=str(value(r,cc,True)).strip()
        if length is not None and (length<=0 or length>=1000000):length=None
        datevalue=numeric(value(r,pc,True)) if pc else None
        planned=(book['epoch']+timedelta(days=int(datevalue))).isoformat() if datevalue is not None and 0<datevalue<200000 else ''
        note='; '.join(str(value(r,c,True)).strip() for c in note_cols if value(r,c,True))
        key=origin(r,mc)[0]
        item=groups.setdefault(key,{'rows':[],'defects':[],'method':m,'length_m':str(length) if length is not None else '', 'composition':comp[:200],'planned_date':planned,'note':note[:3000], 'raw':[]})
        item['rows'].append(r);item['defects'].append({'km':str(km),'number':number,'pipe_section':pipe})
        item['raw'].append({'row':r,'cells':{str(c):v for (rr,c),v in cells.items() if rr==r and c<26}})
        if length is not None and comp and re.fullmatch(r'[\d.,+\s]+',comp):
            parts=[numeric(v) for v in comp.split('+')]
            if all(x is not None for x in parts) and abs(sum(parts)-length)>Decimal('.001'):warnings.append(f'Строка {r}: длина и сумма состава различаются. Исходные значения сохранены.')
    if not groups:raise ValidationError('Не найдены строки дефектов с километром, номером и способом ремонта.')
    if len(groups)>300:raise ValidationError('На листе больше 300 работ. Разделите ведомость на листы.')
    right=next((c for c,v in sorted(headers.items()) if v=='муфта' and c>mc),None);couplings=[]
    serial_col=next((c for c,v in sorted(headers.items()) if 'зав' in v and c>mc),None)
    if right and serial_col:
        length_col=right+1;wall_col=right+2
        warehouse_col=next((c for c,v in sorted(headers.items()) if ('склад' in v or 'хранени' in v) and c>right),None)
        fact_col=next((c for c,v in sorted(headers.items()) if 'факт' in v and c>right),None)
        for r in range(hr+1,max(rr for rr,c in cells)+1):
            if re.search(r'на остатках|итого|купер|комп[оа]зит|изоляци|экскава|гермет',str(value(r,right)),re.I):break
            m=method(value(r,right));length=numeric(value(r,length_col));serial=str(value(r,serial_col)).strip()
            if m not in ('p1','p2','p3','p4','p5u') or length is None or not 0<length<100000 or not serial:continue
            wall=numeric(value(r,wall_col))
            if wall is not None and (wall!=wall.to_integral_value() or not 0<wall<10000):raise ValidationError(f'Строка {r}: толщина стенки муфты должна быть целым положительным числом.')
            fact=numeric(value(r,fact_col)) if fact_col else None
            installed=(book['epoch']+timedelta(days=int(fact))).isoformat() if fact is not None and 0<fact<200000 else ''
            couplings.append({'row':r,'method':m,'length_mm':int(length),'wall_mm':str(wall) if wall is not None and 0<wall<10000 else None,'serial':serial[:100],'warehouse':str(value(r,warehouse_col))[:200] if warehouse_col else '', 'historical_installation':bool(value(r,fact_col)) if fact_col else False,'mount_km':str(value(r,right-1)),'installation_date':installed,'raw':{str(c):v for (rr,c),v in cells.items() if rr==r and c>=right-1}})
    return {'sheet':book['sheet'],'sha256':book['sha256'],'jobs':list(groups.values()),'couplings':couplings,'warnings':list(dict.fromkeys(warnings)),'formulas':book['formulas']}
