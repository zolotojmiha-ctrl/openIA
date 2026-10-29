import json,uuid
from datetime import timedelta
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import ValidationError
from . import test_stock as base
from . import stock,stock_groups as groups,stock_inline as grid,stock_planning as planning,stock_assignment as assignment
from .models import StockItem,StockMovement

class Stock621Tests(TestCase):
    setUpTestData=classmethod(base.StockTests.setUpTestData.__func__)
    wh=base.StockTests.wh;data=base.StockTests.data;item=base.StockTests.item;move=base.StockTests.move;balance=base.StockTests.balance;make_job=base.StockTests.make_job;concrete=base.StockTests.concrete;report=base.StockTests.report
    def row(self,g,serials=None,**changes):
        raw=grid.values(grid.RowForm(user=self.leader,group=g).initial);raw.update({k:str(v) for k,v in changes.items()})
        return {'group':g['token'],'values':raw,'serials':serials or {}}
    def group(self,wh,kind=None):return next(g for g in groups.groups(self.leader) if g['warehouse']==wh and (not kind or g['item'].kind==kind))
    def payload(self,rows,mode='edit',**extra):return {'mode':mode,'operation':str(uuid.uuid4()),'rows':rows,**extra}
    def pieces(self,n=2):
        a=self.wh('Володя Барабинск');planning.receive(self.leader,self.data(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8),a,n,self.today,'Поставка',uuid.uuid4());return a
    def test_inline_multirow_edit_receipt_and_idempotent_retry(self):
        a=self.wh('А');b=self.wh('Б');i=self.item();self.move(i,q=10,dst=a);self.move(i,'transfer',2,a,b)
        legacy=self.item(sku='MUFTA',name='-',kind='coupling',unit='pcs',method='p1',diameter_mm=720,length_mm=1500,wall_mm=10)
        ga=self.group(a);g0=self.group(None)
        p=self.payload([self.row(ga,quantity=7),self.row(g0,warehouse=a.pk,quantity=2,name='Муфта П1',receipt_date=self.today-timedelta(days=1),receipt_origin='Омск')])
        grid.apply(self.leader,p);grid.apply(self.leader,p)
        self.assertEqual((self.balance(a,i),self.balance(b,i)),(7,2));self.assertEqual(self.group(a,'coupling')['quantity'],2)
        legacy.refresh_from_db();self.assertTrue(legacy.individual);self.assertEqual(StockItem.objects.filter(kind='coupling').count(),2)
        self.assertEqual(legacy.receipt_date,self.today-timedelta(days=1));self.assertEqual(legacy.receipt_origin,'Омск')
        self.assertIn('общего списка',StockMovement.objects.filter(item=i).first().note)
    def test_edit_is_atomic_when_one_row_has_invalid_quantity(self):
        a=self.pieces();material=self.item(sku='MATERIAL');self.move(material,q=10,dst=a)
        p=self.payload([self.row(self.group(a,'material'),quantity=7),self.row(self.group(a,'coupling'),quantity='1.5')])
        with self.assertRaises(ValidationError):grid.apply(self.leader,p)
        self.assertEqual(self.balance(a,material),10);self.assertEqual(self.group(a,'coupling')['quantity'],2)
    def test_stale_table_rejects_every_edit(self):
        a=self.wh();i=self.item();self.move(i,q=10,dst=a);p=self.payload([self.row(self.group(a),quantity=7)])
        self.move(i,q=1,dst=a)
        with self.assertRaises(ValidationError):grid.apply(self.leader,p)
        self.assertEqual(self.balance(a,i),11)
    def test_bulk_delete_only_selected_warehouse_and_requires_confirmation(self):
        a=self.wh('А');b=self.wh('Б');i=self.item();self.move(i,q=10,dst=a);self.move(i,'transfer',2,a,b);g=self.group(a)
        p=self.payload([{'group':g['token']}],mode='delete')
        with self.assertRaises(ValidationError):grid.apply(self.leader,p)
        p['confirmed']=True;grid.apply(self.leader,p);grid.apply(self.leader,p)
        self.assertEqual((self.balance(a,i),self.balance(b,i)),(0,2));i.refresh_from_db();self.assertIsNone(i.deleted_at)
    def test_bulk_delete_atomic_when_one_piece_is_reserved(self):
        a=self.pieces();material=self.item(sku='MATERIAL');self.move(material,q=10,dst=a);j=self.make_job()
        g=self.group(a,'coupling');groups.distribute(self.leader,g['token'],{str(j.pk):1})
        p=self.payload([{'group':self.group(a,'material')['token']},{'group':self.group(a,'coupling')['token']}],mode='delete',confirmed=True)
        with self.assertRaises(ValidationError):grid.apply(self.leader,p)
        self.assertEqual(self.balance(a,material),10);self.assertEqual(self.group(a,'coupling')['quantity'],2)
    def test_factory_numbers_can_be_swapped_in_one_save(self):
        a=self.pieces();items=list(StockItem.objects.all())
        for i,number in zip(items,['A','B']):
            data=groups.values(i);data.update(serial=number,coupling=i.coupling);stock.save_item(self.leader,i.pk,i.version,data)
        p=self.payload([self.row(self.group(a),serials={str(items[0].pk):'B',str(items[1].pk):'A'})]);grid.apply(self.leader,p)
        self.assertEqual([StockItem.objects.get(pk=i.pk).serial for i in items],['B','A'])
    def test_date_source_and_catalogue_dimensions_are_editable_in_table(self):
        a=self.wh();i=self.item(kind='coupling',unit='pcs',method='p1',length_mm=1000,wall_mm=8)
        p=self.payload([self.row(self.group(None),warehouse=a.pk,quantity=1,length_mm=1500,receipt_date=self.today-timedelta(days=2),receipt_origin='База')])
        grid.apply(self.leader,p);i.refresh_from_db();self.assertEqual(i.length_mm,1500);self.assertEqual(i.receipt_date,self.today-timedelta(days=2))
    def test_changed_receipt_metadata_also_covers_added_quantity(self):
        a=self.pieces(1);date=self.today-timedelta(days=2)
        grid.apply(self.leader,self.payload([self.row(self.group(a),quantity=3,receipt_date=date,receipt_origin='База 2')]))
        self.assertEqual(StockItem.objects.count(),3)
        self.assertTrue(all(i.receipt_date==date and i.receipt_origin=='База 2' for i in StockItem.objects.all()))
    def test_global_buttons_and_modes_are_visible_and_permission_checked(self):
        a=self.pieces();self.client.force_login(self.leader)
        r=self.client.get(reverse('stock_inventory'));self.assertContains(r,'Редактировать список');self.assertNotContains(r,'>Изменить</a>')
        for mode in ['edit','delete']:
            r=self.client.get(reverse('stock_inventory'),{'mode':mode});self.assertEqual(r.status_code,200);self.assertContains(r,'data-stock-grid')
        self.client.force_login(self.worker);r=self.client.post(reverse('stock_inventory'),{'payload':'{}'});self.assertEqual(r.status_code,403)
    def test_post_validation_errors_are_json_and_do_not_write(self):
        a=self.pieces();self.client.force_login(self.leader);p=self.payload([self.row(self.group(a),quantity='1.5')])
        r=self.client.post(reverse('stock_inventory'),{'payload':json.dumps(p)});self.assertEqual(r.status_code,409);self.assertFalse(r.json()['ok']);self.assertEqual(self.group(a)['quantity'],2)
    def test_assignment_is_a_full_table_and_reserves_without_stock_consumption(self):
        a=self.pieces();j=self.make_job();j.composition='1';j.save();g=self.group(a);self.client.force_login(self.leader)
        r=self.client.get(reverse('stock_distribute'),{'group':g['token']});self.assertContains(r,'Состав:');self.assertContains(r,'Стенка');self.assertNotContains(r,'<select')
        r=self.client.post(reverse('stock_distribute'),{'group':g['token'],'targets':json.dumps({str(j.pk):1})});self.assertEqual(r.status_code,302)
        self.assertEqual((self.group(a)['quantity'],self.group(a)['reserved']),(2,1))
    def test_exact_factory_assignment_preserves_other_piece_reservation(self):
        a=self.pieces();j1=self.make_job();j2=self.make_job();g=self.group(a);items=[m['item'] for m in g['members']]
        planning.reserve(self.leader,items[0].pk,items[0].version,j1.pk);g=self.group(a)
        assignment.save(self.leader,g['token'],str(items[1].pk),{str(j2.pk):1},'FACTORY-2')
        self.assertEqual(StockItem.objects.get(pk=items[0].pk).reserved_job_id,j1.pk);self.assertEqual(StockItem.objects.get(pk=items[1].pk).reserved_job_id,j2.pk)
    def test_zero_stock_does_not_offer_invalid_assignment_form(self):
        i=self.item(kind='coupling',unit='pcs',method='p1',length_mm=1000,wall_mm=8);self.client.force_login(self.leader)
        r=self.client.get(reverse('stock_allocate',args=[i.pk]),follow=True);self.assertContains(r,'Сначала укажите склад и количество')
