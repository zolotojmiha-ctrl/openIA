import sys,zipfile
from xml.etree import ElementTree as ET
source,destination,date=sys.argv[1:];ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
with zipfile.ZipFile(source) as archive:files={n:archive.read(n) for n in archive.namelist()}
sheet=ET.fromstring(files['xl/worksheets/sheet1.xml'])
for row in sheet.findall('s:sheetData/s:row',ns):
    cells={c.get('r'):c for c in row}
    if any('ТЕСТ Материал620'==''.join(t.text or '' for t in c.findall('.//s:t',ns)) for c in row):
        n=row.get('r')
        for column,value in [('U','2'),('V',date),('W','Новая база')]:
            cell=cells.get(column+n)
            if cell is None:cell=ET.SubElement(row,'{'+ns['s']+'}c',r=column+n)
            cell.clear();cell.set('r',column+n);cell.set('t','inlineStr');ET.SubElement(ET.SubElement(cell,'{'+ns['s']+'}is'),'{'+ns['s']+'}t').text=value
files['xl/worksheets/sheet1.xml']=ET.tostring(sheet,encoding='utf-8',xml_declaration=True)
with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as archive:
    for name,data in files.items():archive.writestr(name,data)
