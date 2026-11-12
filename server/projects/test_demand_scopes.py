from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from repairs.models import Section
from repairs.importer import read_book
from django.core.files.uploadedfile import SimpleUploadedFile
from . import test_stock as fixture
from .models import StockMovement

class DemandScopeTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    make_job=fixture.StockTests.make_job
    wh=fixture.StockTests.wh
    data=fixture.StockTests.data
    item=fixture.StockTests.item
    move=fixture.StockTests.move

    def test_sections_stay_separate_in_both_reports_and_excel_with_shared_supply_once(self):
        self.section.diameter_mm=720;self.section.save()
        other=Section.objects.create(project_object=self.obj,rnu='ТЕСТ',pipeline='ТЕСТ',name='Другой участок',diameter_mm=720)
        first=self.make_job();second=self.make_job()
        second.section=other;second.save()
        wh=self.wh()
        item=self.item(kind='coupling',unit='pcs',method='p2',diameter_mm=720,length_mm=1000,wall_mm=8)
        self.move(item,dst=wh,q=1)
        self.client.force_login(self.leader)
        movements=StockMovement.objects.count()
        for route,key in [('stock_coverage','rows'),('repair_materials','grouped')]:
            response=self.client.get(reverse(route))
            rows=[row for row in response.context[key] if route=='stock_coverage' or row['unit']=='шт.']
            self.assertEqual(len(rows),1)
            self.assertTrue({row['section'] for row in rows}.issubset({self.section.name,other.name}))
            self.assertEqual(sum(row['missing'] for row in rows),1)
            self.assertEqual(sum(row['missing'] for row in rows),1)
            self.assertContains(response,'Участок: '+rows[0]['section'])
        for route,sheet,col,query in [('stock_coverage','Муфты',7,'?export=xlsx'),('repair_materials_export','Потребность',5,'')]:
            content=self.client.get(reverse(route)+query).content
            cells=read_book(SimpleUploadedFile('report.xlsx',content),sheet_name=sheet)['cells']
            self.assertEqual(cells[(1,col)],'Участок')
            self.assertEqual(len([v for (r,c),v in cells.items() if c==col and r>1]),1)
        self.assertEqual(StockMovement.objects.count(),movements)
