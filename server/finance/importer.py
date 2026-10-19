"""Read the supplied report layout as data, without running formulas or external links."""
import hashlib
import io
import re
import zipfile
from decimal import Decimal,InvalidOperation
from xml.etree import ElementTree as ET
from django.core.exceptions import ValidationError

NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}

def parse_report(upload):
    data=upload.read(2*1024*1024+1);upload.seek(0)
    if len(data)>2*1024*1024:raise ValidationError('Справка должна быть не больше 2 МБ.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if len(z.infolist())>200 or sum(x.file_size for x in z.infolist())>10*1024*1024:raise ValidationError('Слишком большой состав XLSX.')
            strings=[]
            if 'xl/sharedStrings.xml' in z.namelist():
                root=ET.fromstring(z.read('xl/sharedStrings.xml'))
                strings=[''.join(t.text or '' for t in si.findall('.//s:t',NS)) for si in root.findall('s:si',NS)]
            root=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            rows=[]
            for row in root.findall('s:sheetData/s:row',NS):
                values={}
                for c in row.findall('s:c',NS):
                    raw=c.find('s:v',NS);value=raw.text if raw is not None else ''
                    if c.get('t')=='s':value=strings[int(value)]
                    elif c.get('t')=='inlineStr':value=''.join(t.text or '' for t in c.findall('.//s:t',NS))
                    values[re.sub('[0-9]','',c.get('r',''))]=value
                if values.get('A'):rows.append(values)
    except (zipfile.BadZipFile,KeyError,ET.ParseError,ValueError,IndexError) as e:raise ValidationError('Не удалось прочитать структуру XLSX.') from e
    blocks=[];block=None;details=False;totals=[]
    fields={'получен аванс':'advance_received','закрыто по кс':'accepted','получили':'paid_for_work','зачтено аванса':'advance_offset','остаток аванса':'declared_advance_balance','итого не выплачено':'declared_pending'}
    def amount(value):
        try:
            value=Decimal(str(value).replace(' ','').replace(',','.'))
            if not value.is_finite() or value<0 or value>Decimal('9999999999999999'):raise InvalidOperation
            return value.quantize(Decimal('.01'))
        except InvalidOperation as e:raise ValidationError('В справке некорректная сумма.') from e
    for row in rows:
        title=row['A'].strip();normalized=' '.join(title.lower().split()).rstrip(':')
        if re.search(r'\d+\s+дефект',normalized):
            block={'title':title,'total_defects':int(re.search(r'(\d+)\s+дефект',normalized).group(1)),'lines':[]};blocks.append(block);details=False;continue
        if normalized.startswith('итого остаток по авансам'):
            totals.append({'name':title,'value':amount(row.get('B',''))});block=None;continue
        if block is None:
            if normalized=='итого не выплачено':totals.append({'name':title,'value':amount(row.get('B',''))})
            continue
        if normalized.startswith('устранено'):
            block['repaired_defects']=int(amount(row.get('B','')));continue
        if normalized.startswith('выполнено по факту'):
            details=True;continue
        if normalized in fields:
            block[fields[normalized]]=amount(row.get('B',''));continue
        if details and row.get('B'):block['lines'].append({'title':title,'amount':amount(row['B'])})
    required=set(fields.values())|{'total_defects','repaired_defects'}
    if not blocks or any(required-set(b) for b in blocks):raise ValidationError('Не найдены все показатели. Ожидаются блоки справки с авансом, КС, выплатами, зачётом и неоплаченным выполнением.')
    for b in blocks:
        if b['repaired_defects']>b['total_defects']:raise ValidationError('Устранённых дефектов больше общего количества.')
        b['advance_difference']=b['declared_advance_balance']-(b['advance_received']-b['advance_offset'])
        b['pending_difference']=b['declared_pending']-sum((x['amount'] for x in b['lines']),Decimal('0'))
    return {'sha256':hashlib.sha256(data).hexdigest(),'blocks':blocks,'totals':totals}
