"""Bounded XLSX reference reader. No formula execution or external resources."""
import io,posixpath,re,zipfile,hashlib
from decimal import Decimal,InvalidOperation
from xml.etree import ElementTree as ET
from django.core.exceptions import ValidationError
from .importer import NS,REL,col_number

MAX_FILE=10*1024*1024

def number(value):
    text=str(value or '').strip()
    if not re.fullmatch(r'[+-]?\d{1,15}(?:[.,]0+)?',text):return ''
    return str(int(Decimal(text.replace(',','.'))))

def metres(value):
    try:
        text=str(value).strip().replace(',','.')
        if len(text)>60:return None
        value=Decimal(text)
        return str(value.quantize(Decimal('.000001'))) if value.is_finite() and 0<=value<=10000000 else None
    except (InvalidOperation,ValueError):return None

def parse(upload):
    raw=upload.read(MAX_FILE+1);upload.seek(0)
    if not raw or len(raw)>MAX_FILE:raise ValidationError('Нужна раскладка XLSX до 10 МБ.')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            entries=z.infolist()
            if len(entries)>500 or len({e.filename for e in entries})!=len(entries) or sum(e.file_size for e in entries)>120*1024*1024:raise ValueError
            def xml_bytes(path,limit):
                if z.getinfo(path).file_size>limit:raise ValueError
                data=z.read(path)
                if b'\x00' in data or b'<!DOCTYPE' in data or b'<!ENTITY' in data:raise ValueError
                return data
            small=lambda path:ET.fromstring(xml_bytes(path,8*1024*1024))
            strings=[]
            if 'xl/sharedStrings.xml' in z.namelist():strings=[''.join(t.text or '' for t in s.findall('.//s:t',NS)) for s in small('xl/sharedStrings.xml').findall('s:si',NS)]
            rels={r.get('Id'):r for r in small('xl/_rels/workbook.xml.rels')}
            sheets=small('xl/workbook.xml').findall('s:sheets/s:sheet',NS)
            if len(sheets)>12:raise ValueError
            candidates=[]
            for sheet in sheets:
                rel=rels[sheet.get('{'+REL+'}id')]
                if rel.get('TargetMode')=='External':raise ValueError
                target=rel.get('Target','');path=target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/'+target)
                if not path.startswith('xl/worksheets/') or '..' in path.split('/'):raise ValueError
                data=xml_bytes(path,80*1024*1024);rows=[];header=False
                # Release each row; avoid a full million-cell ElementTree/dictionary.
                for _,node in ET.iterparse(io.BytesIO(data),events=('end',)):
                    if node.tag!='{'+NS['s']+'}row':continue
                    row=int(node.get('r','0'))
                    if not 0<row<=30000:raise ValueError
                    cells={};formulas=set()
                    for cell in node.findall('s:c',NS):
                        m=re.fullmatch(r'([A-Z]{1,3})(\d{1,5})',cell.get('r',''))
                        if not m or int(m[2])!=row or col_number(m[1])>200:raise ValueError
                        col=col_number(m[1])
                        if col not in (5,6,7,17,18,25):continue
                        value=cell.findtext('s:v','',NS)
                        if cell.get('t')=='s':value=strings[int(value)]
                        elif cell.get('t')=='inlineStr':value=''.join(t.text or '' for t in cell.findall('.//s:t',NS))
                        if len(value)>1000:raise ValueError
                        cells[col]=value
                        if cell.find('s:f',NS) is not None:formulas.add(col)
                    if row<=10 and 'номер секции' in cells.get(5,'').lower() and 'начала секции' in cells.get(6,'').lower() and 'длина секции' in cells.get(7,'').lower():header=True;node.clear();continue
                    if header:
                        key=number(cells.get(5))
                        if key:
                            if not cells.get(6) and not cells.get(7):node.clear();continue
                            start=metres(cells.get(6));length=metres(cells.get(7))
                            if formulas.intersection((5,6,7,18)):start=None;length=None
                            if start is None and length is None and not formulas:node.clear();continue
                            rows.append([key,start,length,number(cells.get(18)),str(cells.get(25,'')),row,metres(cells.get(17))])
                    node.clear()
                if header and rows:candidates.append({'sheet':sheet.get('name'),'rows':rows})
            if len(candidates)!=1:raise ValidationError('В книге должен быть один лист с колонками «Номер секции», «Дистанция начала секции», «Длина секции». Листы дефектов не используются.')
            result=candidates[0]
            if not 2<=len(result['rows'])<=25000:raise ValueError
            result['sha256']=hashlib.sha256(raw).hexdigest()
            return result
    except ValidationError:raise
    except (ValueError,KeyError,IndexError,ET.ParseError,zipfile.BadZipFile,OverflowError,OSError,InvalidOperation):
        raise ValidationError('Не удалось проверить раскладку XLSX. Формулы, неоднозначные листы и повреждённые файлы не используются для расчёта.') from None
