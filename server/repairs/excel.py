"""Small, explicit XLSX interchange format. One repair code is one construction."""
import io
import re
import zipfile
from decimal import Decimal
from xml.etree import ElementTree as ET
from django.core.exceptions import ValidationError
from accounts.models import normalize_phone
from .importer import numeric, method

HEADERS=['Код ремонта','Км','№ секции','№ дефекта','Тип конструкции','Длина, м','Толщина стенки, мм','Мастер (телефон)','ID участка','Объект','Участок','Состояние','Координаты результата','Причина','Пояснение']
NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'

def workbook(rows):
    ET.register_namespace('',NS)
    sheet=ET.Element('{'+NS+'}worksheet')
    data=ET.SubElement(sheet,'{'+NS+'}sheetData')
    for r,values in enumerate([HEADERS,*rows],1):
        row=ET.SubElement(data,'{'+NS+'}row',r=str(r))
        for c,value in enumerate(values,1):
            letters='';n=c
            while n:n,k=divmod(n-1,26);letters=chr(65+k)+letters
            cell=ET.SubElement(row,'{'+NS+'}c',r=f'{letters}{r}',t='inlineStr')
            text=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',str(value) if value is not None else '')
            ET.SubElement(ET.SubElement(cell,'{'+NS+'}is'),'{'+NS+'}t').text=text
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml',f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Дефекты" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml',ET.tostring(sheet,encoding='utf-8',xml_declaration=True))
    return stream.getvalue()

def parse_standard(book):
    cells=book['cells'];header={str(v).strip():c for (r,c),v in cells.items() if r==1}
    if not set(HEADERS[:8]).issubset(header):
        raise ValidationError('Не найдены обязательные столбцы шаблона openIA. Скачайте шаблон Excel.')
    if book['formulas']:
        raise ValidationError('В шаблоне используйте значения, а не формулы: скопируйте и вставьте как значения.')
    def value(row,name):return str(cells.get((row,header.get(name)), '')).strip()
    groups={};seen=set()
    for row in sorted({r for r,c in cells if r>1}):
        if not any(value(row,h) for h in HEADERS[:8]):continue
        code=value(row,'Код ремонта');number=value(row,'№ дефекта');pipe=value(row,'№ секции')
        km=numeric(value(row,'Км'));m=method(value(row,'Тип конструкции'))
        if value(row,'Тип конструкции').lower() in ('другой способ','other'):m='other'
        if not code or len(code)>80 or not number or len(number)>80 or len(pipe)>80:
            raise ValidationError(f'Строка {row}: укажите код ремонта и номер дефекта длиной до 80 символов; номер секции — до 80 символов.')
        if km is None or not 0<=km<1000000 or km.as_tuple().exponent < -6 or not m:
            raise ValidationError(f'Строка {row}: проверьте километр (до 6 знаков после запятой) и тип конструкции: П1–П5У, шлифовка или вырезка.')
        identity=(km,number)
        if identity in seen:raise ValidationError(f'Строка {row}: дефект на этом километре повторяется. Один дефект не может входить в две группы.')
        seen.add(identity)
        specs=[]
        for name,places,upper in [('Длина, м',3,Decimal('1000000')),('Толщина стенки, мм',2,Decimal('10000'))]:
            text=value(row,name);v=numeric(text) if text else None
            if text and (v is None or not 0<v<upper or v.as_tuple().exponent < -places):
                raise ValidationError(f'Строка {row}: некорректное поле «{name}».')
            specs.append(str(v) if v is not None else '')
        if m.startswith('p') and not specs[0]:raise ValidationError(f'Строка {row}: укажите длину конструкции.')
        phone=value(row,'Мастер (телефон)')
        if phone:phone=normalize_phone(phone)
        signature=(km,m,*specs,phone,value(row,'ID участка'))
        item=groups.setdefault(code,{'signature':signature,'code':code,'rows':[],'defects':[],'method':m,'length_m':specs[0],'wall_mm':specs[1],'master_phone':phone,'section_id':value(row,'ID участка'),'composition':'','planned_date':'','note':'','raw':[]})
        if item['signature']!=signature:
            raise ValidationError(f'Строка {row}: в группе «{code}» различаются километр, конструкция, размеры, участок или мастер. Для другого ремонта укажите другой код.')
        if len(item['defects'])>=30:raise ValidationError('В одной группе допускается до 30 дефектов.')
        item['rows'].append(row);item['defects'].append({'km':str(km),'number':number,'pipe_section':pipe})
        item['raw'].append({'row':row,'cells':{str(c):v for (r,c),v in cells.items() if r==row}})
    if not groups or len(groups)>300:raise ValidationError('В файле должно быть от 1 до 300 групп ремонта.')
    jobs=list(groups.values())
    for item in jobs:item.pop('signature')
    return {'jobs':jobs,'couplings':[],'warnings':['Состояние, координаты и причины в выгрузке — справочные. Импорт не меняет результаты работ и историю.'],'sheet':book['sheet'],'sha256':book['sha256'],'format':'openia-v1'}
