import io,time,uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from . import test_stock as base
from . import stock,stock_planning as planning,stock_groups as groups,stock_exchange as exchange
from .models import StockItem,StockBalance,StockMovement

class Stock620Tests(TestCase):
    setUpTestData=classmethod(base.StockTests.setUpTestData.__func__)
    wh=base.StockTests.wh;data=base.StockTests.data;item=base.StockTests.item;move=base.StockTests.move
    balance=base.StockTests.balance;make_job=base.StockTests.make_job;concrete=base.StockTests.concrete;crew=base.StockTests.crew;report=base.StockTests.report
    def tables(self):
        self.client.force_login(self.leader)
        response=self.client.get(reverse('stock_inventory_export'));self.assertEqual(response.status_code,200)
        return exchange.parse(io.BytesIO(response.content))
    def g(self,wh):return next(g for g in groups.groups(self.leader) if g['warehouse'] and g['warehouse'].pk==wh.pk)
    def pieces(self,n=10,source='База 1'):
        wh=self.wh();planning.receive(self.leader,self.data(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8,receipt_origin=source),wh,n,self.today-timedelta(days=10),'Поставка',uuid.uuid4());return wh
    def test_scoped_delete_keeps_other_warehouse(self):
        a=self.wh('А');b=self.wh('Б');i=self.item();self.move(i,q=10,dst=a);self.move(i,'transfer',2,a,b)
        self.client.force_login(self.leader);url=reverse('stock_delete',args=[i.pk]);res=self.client.get(url,{'warehouse':a.pk})
        self.assertContains(res,'А:');self.assertNotContains(res,'Б:')
        self.assertEqual(self.client.post(url,{'warehouse_id':a.pk,'snapshot':res.context['form'].initial['snapshot'],'expected_version':i.version,'note':'Ошибка на А'}).status_code,302)
        self.assertEqual((self.balance(a,i),self.balance(b,i)),(0,2));self.assertTrue(stock.items(self.leader).filter(pk=i.pk).exists())
    def test_global_delete_requires_explicit_all_warehouses_confirmation(self):
        a=self.wh('А');b=self.wh('Б');i=self.item();self.move(i,q=10,dst=a);self.move(i,'transfer',2,a,b)
        with self.assertRaises(ValidationError):planning.delete(self.leader,i.pk,i.version,'Не тот склад')
        self.assertEqual(self.balance(b,i),2)
        self.client.force_login(self.leader);url=reverse('stock_delete',args=[i.pk]);res=self.client.get(url)
        self.assertContains(res,'А:');self.assertContains(res,'Б:')
        payload={'snapshot':res.context['form'].initial['snapshot'],'expected_version':i.version,'note':'Удалить все','all_warehouses':'on'}
        self.assertEqual(self.client.post(url,payload).status_code,302);self.assertEqual(self.balance(b,i),0)
    def test_delete_snapshot_rejects_concurrent_movement(self):
        a=self.wh();i=self.item();self.move(i,q=10,dst=a);token=planning.delete_snapshot(self.leader,i,a.pk)
        self.move(i,q=1,dst=a)
        with self.assertRaises(ValidationError):planning.delete(self.leader,i.pk,i.version,'Ошибка',a.pk,snapshot=token)
        self.assertEqual(self.balance(a,i),11)
    def test_issue_reserve_return_and_explicit_reassignment_agree(self):
        c,i=self.concrete();a=self.wh();crew=self.crew();b=self.wh('Бригада',brigade_assignment=crew);j=self.make_job();j2=self.make_job()
        self.move(i,q=1,dst=a);self.move(i,'issue',1,a,b,job=j)
        self.assertEqual((self.g(b)['reserved'],self.g(b)['free']),(1,0))
        i.refresh_from_db()
        with self.assertRaises(ValidationError):planning.reserve(self.leader,i.pk,i.version,j2.pk)
        groups.distribute(self.leader,self.g(b)['token'],{str(j2.pk):1})
        self.report(j2,[c]);self.assertEqual(self.balance(b,i),0)
    def test_generic_batch_issued_to_job_is_reserved_as_individual_pieces(self):
        a=self.wh();crew=self.crew();b=self.wh('Бригада',brigade_assignment=crew)
        i=self.item(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8);j=self.make_job();j.length_m=3;j.composition='1+1+1';j.save()
        self.move(i,q=10,dst=a);self.move(i,'issue',3,a,b,job=j)
        self.assertEqual((self.g(a)['free'],self.g(b)['reserved'],self.g(b)['free']),(7,3,0))
        self.assertEqual(len(stock.prepared_couplings(j)),3)

    def test_bulk_transfer_three_and_idempotent_retry(self):
        a=self.pieces();b=self.wh('Б');g=self.g(a);token=uuid.uuid4()
        for _ in range(2):groups.transfer(self.leader,g['token'],b.pk,3,self.today,'Переместить 3',token)
        self.assertEqual((self.g(a)['quantity'],self.g(b)['quantity']),(7,3))
        with self.assertRaises(ValidationError):groups.transfer(self.leader,g['token'],b.pk,4,self.today,'Переместить 3',token)
    def test_excel_nine_hours_old_accepted_but_changed_data_rejected(self):
        a=self.pieces(1);tables=self.tables();now=time.time()
        with patch('django.core.signing.time.time',return_value=now+9*3600):exchange.apply(self.leader,tables)
        i=StockItem.objects.get();self.move(i,'loss',1,a)
        with self.assertRaises(ValidationError):exchange.apply(self.leader,tables)
    def test_excel_repeat_delivery_preserves_real_date_and_origin(self):
        a=self.pieces(1);tables=self.tables()
        tables['Остатки'].append({'row':3,'values':['НОВАЯ-1','',a.name,'ТЕСТ Материал','Муфта','шт.','П2','','1000','8','','','','','','','2','нет',str(self.today-timedelta(days=3)),'База 2']})
        exchange.apply(self.leader,tables)
        self.assertEqual(self.g(a)['quantity'],3)
        items=StockItem.objects.filter(receipt_origin='База 2');self.assertEqual(items.count(),2);self.assertTrue(all(i.receipt_date==self.today-timedelta(days=3) for i in items))
    def test_excel_repeat_delivery_columns(self):
        a=self.pieces(1);tables=self.tables();r=tables['Остатки'][0]['values'];r[20]='2';r[21]=str(self.today-timedelta(days=2));r[22]='База 3'
        exchange.apply(self.leader,tables);self.assertEqual(self.g(a)['quantity'],3);self.assertEqual(StockItem.objects.filter(receipt_origin='База 3').count(),2)
        item=self.item();self.move(item,q=2,dst=a);tables=self.tables()
        for e in tables['Остатки']:
            r=e['values']
            if r[4]=='Материал':r[3]='Новое имя';r[20]='3';r[21]=str(self.today);r[22]='Поставка после переименования'
        exchange.apply(self.leader,tables)
        renamed=[g for g in groups.groups(self.leader) if g['item'].name=='Новое имя']
        self.assertEqual(len(renamed),1);self.assertEqual(renamed[0]['quantity'],5)
    def test_excel_partial_transfer_and_whole_row_transfer(self):
        a=self.pieces(10);b=self.wh('Б');tables=self.tables();key=tables['Остатки'][0]['values'][0]
        tables['Перемещения']=[{'row':2,'values':[key,b.name,'3',str(self.today),'нет','Три муфты']}]
        exchange.apply(self.leader,tables);self.assertEqual((self.g(a)['quantity'],self.g(b)['quantity']),(7,3))
        tables=self.tables()
        for e in tables['Остатки']:
            if e['values'][2]==a.name:e['values'][2]=b.name
        exchange.apply(self.leader,tables);self.assertEqual(self.g(b)['quantity'],10)
    def test_excel_attach_catalogue_without_duplicate(self):
        a=self.wh();i=self.item(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8);tables=self.tables();r=tables['Остатки'][0]['values'];r[2]=a.name;r[16]='1'
        exchange.apply(self.leader,tables);self.assertEqual(StockItem.objects.count(),1);self.assertEqual(self.balance(a,i),1)
    def test_excel_whole_transfer_preserves_unchanged_destination_in_both_orders(self):
        a=self.wh('А');b=self.wh('Б');i=self.item();self.move(i,q=10,dst=a);self.move(i,'transfer',2,a,b)
        tables=self.tables()
        for e in tables['Остатки']:
            if e['values'][2]==a.name:e['values'][2]=b.name
        exchange.apply(self.leader,tables)
        self.assertEqual((self.balance(a,i),self.balance(b,i)),(0,10))
        self.move(i,'transfer',8,b,a);tables=self.tables();tables['Остатки'].reverse()
        for e in tables['Остатки']:
            if e['values'][2]==a.name:e['values'][2]=b.name
        exchange.apply(self.leader,tables)
        self.assertEqual((self.balance(a,i),self.balance(b,i)),(0,10))
    def test_excel_whole_transfer_new_assignment_keeps_destination_reservation(self):
        a=self.pieces(2);b=self.wh('Я');j1=self.make_job();j2=self.make_job()
        groups.transfer(self.leader,self.g(a)['token'],b.pk,1,self.today,'Одна на Я')
        groups.distribute(self.leader,self.g(b)['token'],{str(j1.pk):1})
        tables=self.tables();key=next(e['values'][0] for e in tables['Остатки'] if e['values'][2]==a.name)
        for e in tables['Остатки']:
            if e['values'][0]==key:e['values'][2]=b.name
        tables['Распределение'].append({'row':3,'values':[key,planning.job_label(j2),'1']})
        exchange.apply(self.leader,tables);g=self.g(b)
        self.assertEqual((g['quantity'],g['reserved']),(2,2))
        self.assertEqual({d['job'].pk:int(d['quantity']) for d in g['distribution']},{j1.pk:1,j2.pk:1})
    def test_generic_batch_bulk_transfer_splits_only_moved_pieces(self):
        a=self.wh('А');b=self.wh('Б');i=self.item(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8);self.move(i,q=10,dst=a)
        ids=groups.transfer(self.leader,self.g(a)['token'],b.pk,3,self.today,'Три из партии')
        self.assertEqual(len(ids),3);self.assertEqual((self.g(a)['quantity'],self.g(b)['quantity']),(7,3))
        self.assertEqual(self.balance(a,i),7);self.assertEqual(self.balance(b,i),0)
        self.assertTrue(all(StockItem.objects.get(pk=pk).individual for pk in ids))
    def test_excel_exact_piece_assignment_swaps_without_shuffle_or_overbooking(self):
        a=self.pieces(2);i1,i2=list(StockItem.objects.all());j1=self.make_job();j2=self.make_job()
        planning.reserve(self.leader,i1.pk,i1.version,j1.pk);planning.reserve(self.leader,i2.pk,i2.version,j2.pk)
        tables=self.tables()
        for e in tables['Изделия']:
            r=e['values'];r[9]=planning.job_label(j2 if r[1]==str(i1.pk) else j1)
        exchange.apply(self.leader,tables);i1.refresh_from_db();i2.refresh_from_db();self.assertEqual((i1.reserved_job_id,i2.reserved_job_id),(j2.pk,j1.pk))
    def test_stock_only_user_roundtrip_preserves_invisible_reservation(self):
        from accounts.models import AccessGrant
        a=self.pieces(1);i=StockItem.objects.get();j=self.make_job();planning.reserve(self.leader,i.pk,i.version,j.pk)
        AccessGrant.objects.create(user=self.supply,permission='stock.manage',granted_by=self.leader)
        self.client.force_login(self.supply);response=self.client.get(reverse('stock_inventory_export'));tables=exchange.parse(io.BytesIO(response.content))
        self.assertEqual(tables['Остатки'][0]['values'][17],'нет')
        exchange.apply(self.supply,tables);i.refresh_from_db();self.assertEqual(i.reserved_job_id,j.pk)
        g=groups.groups(self.supply)[0]
        with self.assertRaises(ValidationError):groups.distribute(self.supply,g['token'],{})

    def test_archive_excluded_by_default_and_query_count_bounded(self):
        a=self.pieces(100)
        with CaptureQueriesContext(connection) as captured:groups.groups(self.leader)
        self.assertLess(len(captured),20)
        old=self.item(sku='ARCHIVE');old.archived=True;old.save()
        self.assertFalse(any(m['item'].pk==old.pk for g in groups.groups(self.leader) for m in g['members']))
        self.assertTrue(any(m['item'].pk==old.pk for g in groups.groups(self.leader,include_archived=True) for m in g['members']))

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.conf import settings
from .models import LegalEntity,COMPANY_TAX_ID
import os,subprocess,json,sys
class Stock620Browser(StaticLiveServerTestCase):
    def test_bulk_transfer_scoped_delete_and_real_delivery_excel(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'Тест'})
        base.StockTests.setUpTestData.__func__(type(self));self.leader.set_password('123456');self.leader.save()
        a=stock.save_warehouse(self.leader,None,None,{'name':'Склад А'});b=stock.save_warehouse(self.leader,None,None,{'name':'Склад Б'})
        planning.receive(self.leader,{'sku':'','name':'ТЕСТ Муфта620','kind':'coupling','unit':'pcs','method':'p2','length_mm':1000,'wall_mm':8,'diameter_mm':720,'archived':False},a,10,self.today,'Поставка',uuid.uuid4())
        i=stock.save_item(self.leader,None,None,{'sku':'M620','name':'ТЕСТ Материал620','kind':'material','unit':'kg','method':'','length_mm':None,'wall_mm':None,'diameter_mm':None,'archived':False,'coupling':None})
        stock.move(self.leader,i.pk,'receipt',10,None,a.pk,self.today,'Поставка',uuid.uuid4());stock.move(self.leader,i.pk,'transfer',2,a.pk,b.pk,self.today,'Два на Б',uuid.uuid4())
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/stock620-browser.cjs')],env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_PYTHON':sys.executable,'ARMADA_STOCK620':json.dumps({'a':str(a.pk),'b':str(b.pk),'date':str(self.today-timedelta(days=3))})},capture_output=True,text=True,timeout=100)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(StockBalance.objects.get(item=i,warehouse=b).quantity,2)
        self.assertEqual(sum(StockBalance.objects.filter(warehouse=b,item__name='ТЕСТ Материал620').values_list('quantity',flat=True)),4)

from django.test import TransactionTestCase
from django.db.migrations.executor import MigrationExecutor
class Stock620Upgrade(TransactionTestCase):
    def test_legacy_issue_keeps_quantity_and_gets_authoritative_reservation(self):
        executor=MigrationExecutor(connection);executor.migrate([('projects','0021_alter_stockitem_serial')])
        try:
            LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'Тест'})
            base.StockTests.setUpTestData.__func__(type(self))
            from repairs.models import Job,Defect,JobDefect,Coupling
            job=Job.objects.create(contract=self.contract,section=self.section,method='p2',length_m=1,wall_mm=8,created_by=self.leader)
            d=Defect.objects.create(contract=self.contract,section=self.section,kilometre=1,number='620');JobDefect.objects.create(job=job,defect=d)
            coupling=Coupling.objects.create(project_object=self.obj,method='p2',length_mm=1000,wall_mm=8)
            old=executor.loader.project_state([('projects','0021_alter_stockitem_serial')]).apps
            Warehouse=old.get_model('projects','Warehouse');Item=old.get_model('projects','StockItem');Balance=old.get_model('projects','StockBalance');Move=old.get_model('projects','StockMovement')
            a=Warehouse.objects.create(legal_entity_id=self.company.pk,name='А',created_by_id=self.leader.pk);b=Warehouse.objects.create(legal_entity_id=self.company.pk,name='Б',created_by_id=self.leader.pk)
            item=Item.objects.create(legal_entity_id=self.company.pk,sku='OLD620',name='Старая муфта',kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8,coupling_id=coupling.pk,created_by_id=self.leader.pk)
            Balance.objects.create(item=item,warehouse=b,quantity=1)
            Move.objects.create(item=item,kind='receipt',quantity=1,destination=a,date=self.today,digest='old',note='Приёмка',created_by_id=self.leader.pk)
            Move.objects.create(item=item,kind='issue',quantity=1,source=a,destination=b,job_id=job.pk,date=self.today,digest='old2',note='Выдача',created_by_id=self.leader.pk)
            batch=Item.objects.create(legal_entity_id=self.company.pk,sku='OLD-BATCH',name='Партия',kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8,created_by_id=self.leader.pk)
            Balance.objects.create(item=batch,warehouse=a,quantity=7);Balance.objects.create(item=batch,warehouse=b,quantity=3)
            Move.objects.create(item=batch,kind='receipt',quantity=10,destination=a,date=self.today,digest='batch1',note='Приёмка партии',created_by_id=self.leader.pk)
            Move.objects.create(item=batch,kind='issue',quantity=3,source=a,destination=b,job_id=job.pk,date=self.today,digest='batch2',note='Выдача партии',created_by_id=self.leader.pk)
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
        item=StockItem.objects.get(pk=item.pk);self.assertEqual(item.reserved_job_id,job.pk);self.assertEqual(item.reservation_origin,'issue')
        self.assertEqual(item.balances.get().quantity,1);self.assertEqual(item.movements.count(),2)
        g=next(g for g in groups.groups(self.leader) if g['warehouse'].pk==b.pk);self.assertEqual((g['free'],g['reserved']),(0,4))
        self.assertEqual(StockBalance.objects.get(item_id=batch.pk,warehouse_id=a.pk).quantity,7)
        self.assertEqual(StockBalance.objects.get(item_id=batch.pk,warehouse_id=b.pk).quantity,0)
        self.assertEqual(StockItem.objects.filter(reserved_job=job).count(),4)
