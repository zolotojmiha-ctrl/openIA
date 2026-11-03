"""Customer attachment: one sheet, only demand; no inventory or hidden worksheets."""
import io,zipfile
from xml.etree import ElementTree as E
from .report_excel import workbook as base_workbook,NS,CONTENT,column

def workbook(data):
    raw=base_workbook([('Потребность',[],[])])
    source=zipfile.ZipFile(io.BytesIO(raw))
    sheet=E.Element(f'{{{NS}}}worksheet')
    views=E.SubElement(sheet,f'{{{NS}}}sheetViews');view=E.SubElement(views,f'{{{NS}}}sheetView',workbookViewId='0',showGridLines='0')
    E.SubElement(view,f'{{{NS}}}pane',ySplit='5',topLeftCell='A6',activePane='bottomLeft',state='frozen')
    cols=E.SubElement(sheet,f'{{{NS}}}cols')
    for n,width in enumerate([6,14,16,18,18,22],1):E.SubElement(cols,f'{{{NS}}}col',min=str(n),max=str(n),width=str(width),customWidth='1')
    rows=E.SubElement(sheet,f'{{{NS}}}sheetData');merges=[]
    def row(values,style=0,height=28,merge=False):
        n=len(rows)+1;r=E.SubElement(rows,f'{{{NS}}}row',r=str(n),ht=str(height),customHeight='1')
        for c,value in enumerate(values,1):
            cell=E.SubElement(r,f'{{{NS}}}c',r=f'{column(c)}{n}',s=str(4 if c==6 and isinstance(value,int) else style))
            if isinstance(value,int):E.SubElement(cell,f'{{{NS}}}v').text=str(value)
            else:
                cell.set('t','inlineStr');E.SubElement(E.SubElement(cell,f'{{{NS}}}is'),f'{{{NS}}}t').text=str(value)
        if merge:merges.append(f'A{n}:F{n}')
    row(['Потребность в ремонтных конструкциях'],1,38,True)
    row(['для ООО «Армада-Сибирь»'],0,23,True)
    row(['По состоянию на '+data['date']+' · на оставшиеся работы'],5,23,True)
    row([],0,10)
    row(['№','Тип муфты','Диаметр','Длина, мм','Стенка, мм','Количество, шт.'],2,32)
    scope=None
    for n,r in enumerate(data['rows'],1):
        key=(r['object_id'],r['section_id'])
        if key!=scope:
            row([r['object']+' · '+r['section']],3,30,True);scope=key
        row([n,r['method'],r['dn'],int(r['length']),r['wall'],int(r['quantity'])])
    if not data['rows']:row(['Потребности на оставшиеся работы нет.'],0,28,True)
    if merges:
        merge=E.SubElement(sheet,f'{{{NS}}}mergeCells',count=str(len(merges)))
        for ref in merges:E.SubElement(merge,f'{{{NS}}}mergeCell',ref=ref)
    E.SubElement(sheet,f'{{{NS}}}pageMargins',left='.3',right='.3',top='.4',bottom='.4',header='.2',footer='.2')
    E.SubElement(sheet,f'{{{NS}}}pageSetup',paperSize='9',orientation='portrait',fitToWidth='1',fitToHeight='0')
    styles=f'''<styleSheet xmlns="{NS}"><fonts count="4"><font><sz val="11"/><name val="Arial"/></font><font><b/><sz val="18"/><name val="Arial"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Arial"/></font><font><b/><color rgb="FF554184"/><sz val="11"/><name val="Arial"/></font></fonts><fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF65509E"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFEEE9F6"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="6"><xf fontId="0" fillId="0" borderId="0" numFmtId="0" xfId="0"><alignment vertical="center" horizontal="center"/></xf><xf fontId="1" fillId="0" borderId="0" numFmtId="0" xfId="0"/><xf fontId="2" fillId="2" borderId="0" numFmtId="0" xfId="0" applyFill="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf><xf fontId="3" fillId="3" borderId="0" numFmtId="0" xfId="0" applyFill="1"/><xf fontId="3" fillId="3" borderId="0" numFmtId="0" xfId="0" applyFill="1"><alignment horizontal="center" vertical="center"/></xf><xf fontId="0" fillId="0" borderId="0" numFmtId="0" xfId="0"/></cellXfs></styleSheet>'''
    types=E.fromstring(source.read('[Content_Types].xml'))
    E.SubElement(types,f'{{{CONTENT}}}Override',PartName='/xl/styles.xml',ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml')
    rel=E.fromstring(source.read('xl/_rels/workbook.xml.rels'))
    ns='http://schemas.openxmlformats.org/package/2006/relationships'
    E.SubElement(rel,f'{{{ns}}}Relationship',Id='styles',Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles',Target='styles.xml')
    replacements={'xl/worksheets/sheet1.xml':E.tostring(sheet),'xl/styles.xml':styles.encode(),'[Content_Types].xml':E.tostring(types),'xl/_rels/workbook.xml.rels':E.tostring(rel)}
    result=io.BytesIO()
    with zipfile.ZipFile(result,'w',zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():target.writestr(name,replacements.pop(name,source.read(name)))
        for name,value in replacements.items():target.writestr(name,value)
    return result.getvalue()
