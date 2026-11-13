"""Actual-result export. Numeric measurements occur once per common repair."""
import io,zipfile
from xml.etree import ElementTree as ET
from .excel import workbook as basic,NS
from .completion_register import HEADERS


def workbook(rows):
    original=basic(rows,headers=HEADERS,sheet_name='Устранённые дефекты')
    sheet=ET.fromstring(zipfile.ZipFile(io.BytesIO(original)).read('xl/worksheets/sheet1.xml'))
    tag=lambda name:'{'+NS+'}'+name
    views=ET.Element(tag('sheetViews'));view=ET.SubElement(views,tag('sheetView'),workbookViewId='0')
    ET.SubElement(view,tag('pane'),ySplit='1',topLeftCell='A2',activePane='bottomLeft',state='frozen')
    sheet.insert(0,views)
    columns=ET.Element(tag('cols'))
    for index,width in enumerate((28,28,28,12,16,16,38,18,30,30,25,25,22,30,28,22,35),1):
        ET.SubElement(columns,tag('col'),min=str(index),max=str(index),width=str(width),customWidth='1')
    sheet.insert(1,columns)
    for row_index,row in enumerate(rows,2):
        for index in (11,13):
            value=row[index]
            if value is None:continue
            cell=sheet.find(f"{tag('sheetData')}/{tag('row')}[@r='{row_index}']/{tag('c')}[@r='{chr(65+index)}{row_index}']")
            cell.clear();cell.set('r',f'{chr(65+index)}{row_index}');cell.set('t','n')
            ET.SubElement(cell,tag('v')).text=str(value)
    ET.SubElement(sheet,tag('autoFilter'),ref=f'A1:Q{len(rows)+1}')
    result=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source,zipfile.ZipFile(result,'w',zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            target.writestr(name,ET.tostring(sheet,encoding='utf-8',xml_declaration=True) if name=='xl/worksheets/sheet1.xml' else source.read(name))
    return result.getvalue()
