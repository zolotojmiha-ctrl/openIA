"""Small read-only XLSX reports: named sheets, numeric quantities, literal text."""
import io
import re
import zipfile
from decimal import Decimal
from xml.etree import ElementTree as ET

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
OFFICE_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
CONTENT = 'http://schemas.openxmlformats.org/package/2006/content-types'


def column(number):
    result = ''
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def workbook(sheets):
    ET.register_namespace('', NS)
    book = ET.Element(f'{{{NS}}}workbook')
    names = ET.SubElement(book, f'{{{NS}}}sheets')
    relations = ET.Element('Relationships', xmlns=REL)
    types = ET.Element('Types', xmlns=CONTENT)
    ET.SubElement(types, 'Default', Extension='rels', ContentType='application/vnd.openxmlformats-package.relationships+xml')
    ET.SubElement(types, 'Default', Extension='xml', ContentType='application/xml')
    ET.SubElement(types, 'Override', PartName='/xl/workbook.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for index, (name, headers, rows) in enumerate(sheets, 1):
            ET.SubElement(names, f'{{{NS}}}sheet', name=name, sheetId=str(index), attrib={f'{{{OFFICE_REL}}}id': f'rId{index}'})
            ET.SubElement(relations, 'Relationship', Id=f'rId{index}', Type=OFFICE_REL + '/worksheet', Target=f'worksheets/sheet{index}.xml')
            ET.SubElement(types, 'Override', PartName=f'/xl/worksheets/sheet{index}.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
            sheet = ET.Element(f'{{{NS}}}worksheet')
            view = ET.SubElement(ET.SubElement(sheet, f'{{{NS}}}sheetViews'), f'{{{NS}}}sheetView', workbookViewId='0')
            ET.SubElement(view, f'{{{NS}}}pane', ySplit='1', topLeftCell='A2', activePane='bottomLeft', state='frozen')
            data = ET.SubElement(sheet, f'{{{NS}}}sheetData')
            for row_index, values in enumerate([headers, *rows], 1):
                row = ET.SubElement(data, f'{{{NS}}}row', r=str(row_index))
                for col, value in enumerate(values, 1):
                    cell = ET.SubElement(row, f'{{{NS}}}c', r=f'{column(col)}{row_index}')
                    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
                        if not Decimal(value).is_finite():
                            raise ValueError('Report quantities must be finite.')
                        ET.SubElement(cell, f'{{{NS}}}v').text = str(value)
                    else:
                        cell.set('t', 'inlineStr')
                        text = '' if value is None else str(value)
                        ET.SubElement(ET.SubElement(cell, f'{{{NS}}}is'), f'{{{NS}}}t').text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
            if headers:
                ET.SubElement(sheet, f'{{{NS}}}autoFilter', ref=f'A1:{column(len(headers))}{len(rows) + 1}')
            archive.writestr(f'xl/worksheets/sheet{index}.xml', ET.tostring(sheet, encoding='utf-8', xml_declaration=True))
        root = ET.Element('Relationships', xmlns=REL)
        ET.SubElement(root, 'Relationship', Id='rId1', Type=OFFICE_REL + '/officeDocument', Target='xl/workbook.xml')
        for path, element in [('[Content_Types].xml', types), ('_rels/.rels', root), ('xl/workbook.xml', book), ('xl/_rels/workbook.xml.rels', relations)]:
            archive.writestr(path, ET.tostring(element, encoding='utf-8', xml_declaration=True))
    return stream.getvalue()
