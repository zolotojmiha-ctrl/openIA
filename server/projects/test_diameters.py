import io
from django.test import TestCase,SimpleTestCase
from django.core.exceptions import ValidationError
from .diameters import DN_TO_OD,label,physical,DiameterField
from . import test_stock as fixture,stock_exchange
from .p1_coating import row
from repairs.forms import SectionForm

class DiameterRegistryTests(SimpleTestCase):
    def test_approved_choices_keep_physical_values_and_norms(self):
        field=DiameterField()
        self.assertEqual(len(field.choices),13)
        for dn,od in DN_TO_OD.items():
            self.assertEqual(label(od),f'DN {dn}')
            self.assertEqual(physical(f'DN {dn}'),od)
            self.assertEqual(field.clean(str(od)),od)
        self.assertEqual(field.clean('720'),720)
        self.assertEqual(row(720,1000)['wrapper_kg'],'4.1')
        with self.assertRaises(ValidationError):field.clean('700')
        self.assertEqual(physical('720'),720)

class DiameterIntegrationTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    wh=fixture.StockTests.wh
    data=fixture.StockTests.data
    item=fixture.StockTests.item
    move=fixture.StockTests.move

    def test_section_edit_preserves_actual_size(self):
        self.section.diameter_mm=720;self.section.save()
        form=SectionForm({'project_object':str(self.obj.pk),'rnu':self.section.rnu,'pipeline':self.section.pipeline,'name':self.section.name,'diameter_mm':'720'},user=self.leader,instance=self.section)
        self.assertTrue(form.is_valid(),form.errors)
        self.assertIn('DN 700',str(form['diameter_mm']))
        self.assertEqual(form.fields['diameter_mm'].widget.__class__.__name__,'Select')
        form.save();self.section.refresh_from_db()
        self.assertEqual(self.section.diameter_mm,720)

    def test_stock_exchange_shows_dn_and_roundtrips_without_changing_geometry(self):
        from django.test import RequestFactory
        from .models import StockBalance
        from repairs.importer import read_book
        from django.core.files.uploadedfile import SimpleUploadedFile
        wh=self.wh();item=self.item(kind='coupling',unit='pcs',method='p2',diameter_mm=720,length_mm=1000,wall_mm=8,name='Муфта П2 Ø720')
        self.move(item,dst=wh,q=3)
        request=RequestFactory().get('/cabinet/stock/exchange/');request.user=self.leader
        response=stock_exchange.export(request)
        book=read_book(SimpleUploadedFile('test.xlsx',response.content),sheet_name='Остатки')
        self.assertEqual(book['cells'][(2,8)],'DN 700')
        self.assertEqual(book['cells'][(1,8)],'Номинальный диаметр')
        tables=stock_exchange.parse(SimpleUploadedFile('test.xlsx',response.content))
        stock_exchange.apply(self.leader,tables)
        item.refresh_from_db();self.assertEqual(item.diameter_mm,720)
        self.assertEqual(StockBalance.objects.get(item=item).quantity,3)

    def test_unknown_legacy_size_survives_an_unrelated_section_edit(self):
        self.section.diameter_mm=721;self.section.save()
        form=SectionForm({'project_object':str(self.obj.pk),'rnu':self.section.rnu,'pipeline':self.section.pipeline,'name':self.section.name+' новая','diameter_mm':'721'},user=self.leader,instance=self.section)
        self.assertTrue(form.is_valid(),form.errors)
        form.save();self.section.refresh_from_db()
        self.assertEqual(self.section.diameter_mm,721)
