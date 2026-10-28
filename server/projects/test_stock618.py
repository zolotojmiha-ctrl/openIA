import uuid
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from .test_stock import StockTests
from . import stock, stock_planning as planning
from .models import StockItem,StockMovement,LegalEntity,COMPANY_TAX_ID
from repairs.models import CouplingUse,Job

class Stock618Tests(StockTests):
    def receive_piece(self,**changes):
        wh=self.wh();data=self.data(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8,diameter_mm=720,serial='',**changes)
        planning.receive(self.leader,data,wh,1,self.today,'Получено',uuid.uuid4())
        return wh,StockItem.objects.get(individual=True)

    def test_receipt_pieces_and_idempotency(self):
        wh=self.wh();data=self.data(kind='coupling',unit='pcs',method='p2',length_mm=1500,wall_mm=8)
        token=uuid.uuid4()
        planning.receive(self.leader,data,wh,3,self.today,'Поставка',token)
        planning.receive(self.leader,data,wh,3,self.today,'Поставка',token)
        self.assertEqual(StockItem.objects.filter(individual=True).count(),3)
        self.assertEqual(sum(i.balances.get().quantity for i in StockItem.objects.all()),3)
        with self.assertRaises(ValidationError):planning.receive(self.leader,data,wh,4,self.today,'Поставка',token)
        with self.assertRaises(ValidationError):planning.receive(self.leader,data,wh,2,self.today,'Поставка',token)

    def test_reservation_then_install_consumes_once(self):
        wh,i=self.receive_piece();j=self.make_job();self.section.diameter_mm=720;self.section.save()
        item=planning.reserve(self.leader,i.pk,i.version,j.pk)
        self.assertEqual(self.balance(wh,i),1)
        self.assertEqual(stock.prepared_couplings(j),[item.coupling])
        self.assertFalse(CouplingUse.objects.exists())
        rep=self.report(j,[item.coupling]);self.assertEqual(self.balance(wh,i),0)
        self.assertTrue(CouplingUse.objects.filter(job=j).exists())
        with self.assertRaises(ValidationError):planning.reserve(self.leader,item.pk,item.version,None)

    def test_no_double_reservation_and_release(self):
        wh,i=self.receive_piece();j=self.make_job();j2=self.make_job()
        item=planning.reserve(self.leader,i.pk,i.version,j.pk)
        with self.assertRaises(ValidationError):planning.reserve(self.leader,i.pk,item.version,j2.pk)
        with self.assertRaises(ValidationError):planning.reserve(self.leader,i.pk,i.version,None)
        item=planning.reserve(self.leader,i.pk,item.version,None)
        item=planning.reserve(self.leader,i.pk,item.version,j2.pk)
        self.assertEqual(item.reserved_job_id,j2.pk)

    def test_wrong_length_wall_diameter_rejected(self):
        wh,i=self.receive_piece();j=self.make_job();j.length_m=Decimal('1.5');j.save()
        with self.assertRaises(ValidationError):planning.reserve(self.leader,i.pk,i.version,j.pk)
        j.length_m=1;j.wall_mm=10;j.save()
        with self.assertRaises(ValidationError):planning.reserve(self.leader,i.pk,i.version,j.pk)
        j.wall_mm=8;j.save();self.section.diameter_mm=530;self.section.save()
        with self.assertRaises(ValidationError):planning.reserve(self.leader,i.pk,i.version,j.pk)

    def test_delete_removes_balances_keeps_history(self):
        wh,i=self.receive_piece();planning.delete(self.leader,i.pk,i.version,'Ошибочный ввод')
        self.assertEqual(self.balance(wh,i),0);self.assertFalse(stock.items(self.leader).filter(pk=i.pk).exists())
        self.assertEqual(StockMovement.objects.filter(item=i).count(),2)
        self.client.force_login(self.leader);self.assertEqual(self.client.get(reverse('stock_item_edit',args=[i.pk])).status_code,404)

    def test_split_existing_batch(self):
        wh=self.wh();i=self.item(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8)
        self.move(i,q=3,dst=wh);piece=planning.split(self.leader,i.pk,i.version,wh.pk)
        self.assertEqual(self.balance(wh,i),2);self.assertEqual(self.balance(wh,piece),1)
        j=self.make_job();piece=planning.reserve(self.leader,piece.pk,piece.version,j.pk)
        self.assertEqual(piece.reserved_job_id,j.pk)

    def test_coverage_composition_and_reserve(self):
        wh,i=self.receive_piece();self.section.diameter_mm=720;self.section.save()
        j=self.make_job();j.length_m=Decimal('2.5');j.composition='1+1.5';j.save()
        planning.reserve(self.leader,i.pk,i.version,j.pk)
        self.client.force_login(self.leader)
        res=self.client.get(reverse('stock_coverage'));self.assertEqual(res.status_code,200)
        rows=res.context['rows'];self.assertEqual(len(rows),2)
        self.assertEqual(sum(r['missing'] for r in rows),1)
        self.assertEqual(sum(r['reserved'] for r in rows),1)
        j.composition='неизвестно';j.save();res=self.client.get(reverse('stock_coverage'));self.assertEqual(len(res.context['unknown']),1)

    def test_insulation_square_metres_and_no_conversion(self):
        wh=self.wh();data=self.data(kind='insulation',unit='m2')
        planning.receive(self.leader,data,wh,Decimal('12.5'),self.today,'Рулон',uuid.uuid4())
        i=StockItem.objects.get();self.assertEqual(self.balance(wh,i),Decimal('12.5'));self.assertEqual(i.get_unit_display(),'м²')
        with self.assertRaises(ValidationError):stock.save_item(self.leader,None,None,self.data(sku='2',kind='insulation',unit='kg'))

    def test_receipt_into_existing_brigade_holder(self):
        crew=self.crew();wh=self.wh(brigade_assignment=crew)
        data=self.data(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8)
        planning.receive(self.leader,data,wh,1,self.today,'Реальная поставка',uuid.uuid4())
        self.assertEqual(wh.balances.get().quantity,1)

    def test_visible_routes_and_forms(self):
        wh,i=self.receive_piece();self.client.force_login(self.leader)
        for name,args in [('stock_inventory',[]),('stock_coverage',[]),('stock_inventory_export',[]),('stock_receive',[]),('stock_allocate',[i.pk]),('stock_delete',[i.pk]),('stock_detail',[wh.pk])]:
            res=self.client.get(reverse(name,args=args));self.assertEqual(res.status_code,200,(name,res.content[:1000]))
        self.assertContains(self.client.get(reverse('stock_detail',args=[wh.pk])),'Добавить на этот склад')
        res=self.client.post(reverse('stock_receive'),{'warehouse':wh.pk,'quantity':2,'date':str(self.today),'note':'Тест','token':uuid.uuid4(),'name':'','kind':'coupling','unit':'pcs','method':'p1','length_mm':1500,'wall_mm':10,'diameter_mm':720,'serial':''})
        self.assertEqual(res.status_code,302,res.context['form'].errors if res.status_code!=302 else '')
        self.assertEqual(StockItem.objects.filter(individual=True).count(),3)

    def test_stock_permissions(self):
        self.client.force_login(self.worker)
        for name in ('stock_receive','stock_inventory','stock_coverage'):
            self.assertEqual(self.client.get(reverse(name)).status_code,403)
        with self.assertRaises(Exception):planning.receive(self.worker,self.data(),self.wh(),1,self.today,'X',uuid.uuid4())

    def test_catalogue_receipt_form_reuses_existing_card(self):
        wh=self.wh();item=self.item(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8)
        self.client.force_login(self.leader)
        response=self.client.get(reverse('stock_receive'),{'item':item.pk,'warehouse':wh.pk})
        self.assertEqual(response.context['form'].initial['catalogue_item'].pk,item.pk)
        response=self.client.post(reverse('stock_receive'),{'catalogue_item':item.pk,'catalogue_version':item.version,'warehouse':wh.pk,'quantity':1,'date':str(self.today),'note':'Начальный остаток','token':uuid.uuid4(),'name':item.name,'kind':'coupling','unit':'pcs','method':'p2','length_mm':1000,'wall_mm':8,'serial':''})
        self.assertEqual(response.status_code,302,response.context['form'].errors if response.status_code!=302 else '')
        self.assertEqual(StockItem.objects.count(),1);self.assertEqual(self.balance(wh,item),1)

    def test_existing_catalogue_card_becomes_stock_without_duplicate(self):
        wh=self.wh();item=self.item(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8)
        data=self.data(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8,catalogue_item=item,catalogue_version=item.version)
        token=uuid.uuid4();planning.receive(self.leader,data,wh,2,self.today,'Начальное поступление',token)
        self.assertEqual(StockItem.objects.count(),2);self.assertEqual(self.balance(wh,item),1)
        planning.receive(self.leader,data,wh,2,self.today,'Начальное поступление',token)
        self.assertEqual(StockItem.objects.count(),2)

# These assertions cover the late metadata edits and partial installation path.
    def test_source_date_and_factory_number_can_be_completed_later(self):
        wh,i=self.receive_piece(receipt_origin='Сокур')
        j=self.make_job();item=planning.reserve(self.leader,i.pk,i.version,j.pk)
        self.report(j,[item.coupling]);item.refresh_from_db()
        values={k:getattr(item,k) for k in stock.ITEM_FIELDS};values.update(coupling=item.coupling,serial='ДОП-618',receipt_origin='Сокур')
        item=stock.save_item(self.leader,item.pk,item.version,values)
        self.assertEqual(item.coupling.serial,'ДОП-618');self.assertEqual(item.receipt_origin,'Сокур')
        self.assertEqual(self.balance(wh,item),0)

    def test_missing_diameter_has_visible_edit_and_optimistic_conflict(self):
        j=self.make_job();self.client.force_login(self.leader)
        response=self.client.get(reverse('stock_coverage'));self.assertContains(response,'Указать диаметр нефтепровода')
        url=reverse('stock_diameter',args=[self.section.pk])
        self.assertEqual(self.client.post(url,{'expected_diameter':0,'diameter_mm':720}).status_code,302)
        self.section.refresh_from_db();self.assertEqual(self.section.diameter_mm,720)
        self.assertEqual(self.client.post(url,{'expected_diameter':0,'diameter_mm':530}).status_code,409)

    def test_partial_installation_not_in_remaining_need(self):
        wh,i=self.receive_piece();self.section.diameter_mm=720;self.section.save();j=self.make_job();j.length_m=Decimal('2.5');j.composition='1+1.5';j.save()
        item=planning.reserve(self.leader,i.pk,i.version,j.pk)
        from repairs.services import report_work
        report_work(self.leader,j.pk,'result',{'token':uuid.uuid4(),'expected_version':j.version,'performed_date':self.today,'outcome':'partial','method':'p2','length_m':j.length_m,'composition':j.composition,'note':'Часть конструкции','problem':'','latitude':None,'longitude':None,'couplings':[item.coupling],'files':[]})
        self.client.force_login(self.leader);response=self.client.get(reverse('stock_coverage'))
        self.assertEqual(sum(r['need'] for r in response.context['rows']),1)
        self.assertEqual(sum(r['missing'] for r in response.context['rows']),1)
        self.assertEqual(self.balance(wh,item),0)

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.conf import settings
import subprocess, os, json
class Stock618Browser(StaticLiveServerTestCase):
    def test_receipt_reservation_delete_and_square_metres(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'Тест'})
        StockTests.setUpTestData.__func__(type(self))
        wh=stock.save_warehouse(self.leader,None,None,{'name':'Володя Барабинск','location':'Барабинск','archived':False})
        self.section.diameter_mm=720;self.section.save()
        job=Job.objects.create(contract=self.contract,section=self.section,method='p2',length_m=Decimal('1.5'),wall_mm=8,created_by=self.leader)
        from repairs.models import Defect,JobDefect
        defect=Defect.objects.create(contract=self.contract,section=self.section,kilometre=Decimal('153.569'),number='1748');JobDefect.objects.create(job=job,defect=defect)
        self.leader.set_password('123456');self.leader.save()
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/stock618-browser.cjs')],env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_STOCK_FIXTURE':json.dumps({'job':str(job.pk),'warehouse':str(wh.pk)})},capture_output=True,text=True,timeout=100)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(StockItem.objects.filter(reserved_job=job).count(),1)
        self.assertEqual(StockItem.objects.filter(deleted_at__isnull=False).count(),1)

from django.test import TransactionTestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
class Stock618Upgrade(TransactionTestCase):
    def test_617_balances_serial_and_receipt_date_preserved(self):
        executor=MigrationExecutor(connection)
        executor.migrate([('projects','0017_warehouse_served_brigades_warehouse_served_objects')])
        try:
            old=executor.loader.project_state([('projects','0017_warehouse_served_brigades_warehouse_served_objects')]).apps
            Entity=old.get_model('projects','LegalEntity');User=old.get_model('accounts','User');Wh=old.get_model('projects','Warehouse');Item=old.get_model('projects','StockItem');Balance=old.get_model('projects','StockBalance');Move=old.get_model('projects','StockMovement');Area=old.get_model('projects','ProjectObject');Coupling=old.get_model('repairs','Coupling')
            entity,_=Entity.objects.get_or_create(tax_id='5528202398',defaults={'name':'Тест'})
            user=User.objects.create(phone='+79990006180',password='unused',full_name='Тест')
            wh=Wh.objects.create(legal_entity=entity,name='Володя Барабинск',created_by=user)
            area=Area.objects.create(legal_entity=entity,name='Старый участок')
            coupling=Coupling.objects.create(project_object=area,method='p2',length_mm=1000,wall_mm=8,serial='СТАРЫЙ-618')
            item=Item.objects.create(legal_entity=entity,sku='СТАРАЯ',name='Муфта',kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8,coupling=coupling,created_by=user)
            Balance.objects.create(warehouse=wh,item=item,quantity=1)
            Move.objects.create(item=item,kind='receipt',quantity=1,destination=wh,date='2026-08-28',note='Старая поставка',created_by=user,digest='old')
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
        item=StockItem.objects.get(pk=item.pk)
        self.assertTrue(item.individual);self.assertEqual(item.serial,'СТАРЫЙ-618')
        self.assertEqual(str(item.receipt_date),'2026-08-28');self.assertEqual(item.receipt_warehouse_id,wh.pk)
        self.assertEqual(item.balances.get().quantity,1);self.assertEqual(item.movements.count(),1)

from unittest import skipUnless
from django.db import connections, close_old_connections
from concurrent.futures import ThreadPoolExecutor
import threading
@skipUnless(connection.vendor=='postgresql','Требуется PostgreSQL')
class Stock618Concurrency(TransactionTestCase):
    def setUp(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'Тест'})
        StockTests.setUpTestData.__func__(type(self))
        self.wh=stock.save_warehouse(self.leader,None,None,{'name':'Тестовый склад'})
        planning.receive(self.leader,{'sku':'ignore','name':'Муфта','kind':'coupling','unit':'pcs','method':'p2','diameter_mm':720,'length_mm':1000,'wall_mm':8,'archived':False},self.wh,1,self.today,'Тест',uuid.uuid4())
        self.item=StockItem.objects.get(individual=True)
        from repairs.models import Job,Defect,JobDefect
        self.jobs=[]
        for n in (1,2):
            j=Job.objects.create(contract=self.contract,section=self.section,method='p2',length_m=1,wall_mm=8,created_by=self.leader)
            d=Defect.objects.create(contract=self.contract,section=self.section,kilometre=n,number=str(n));JobDefect.objects.create(job=j,defect=d);self.jobs.append(j)
    def test_two_allocations_cannot_reserve_one_piece(self):
        barrier=threading.Barrier(2)
        def run(n):
            close_old_connections()
            try:
                barrier.wait(timeout=10);planning.reserve(self.leader,self.item.pk,self.item.version,self.jobs[n].pk);return 'saved'
            except ValidationError:return 'conflict'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(run,[0,1]))
        self.assertCountEqual(result,['saved','conflict']);self.assertEqual(self.item.balances.get().quantity,1)
