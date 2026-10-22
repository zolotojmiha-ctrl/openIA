import secrets,threading,uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from django.db import connection,connections,close_old_connections
from django.test import TransactionTestCase
from django.core.exceptions import ValidationError
from django.utils import timezone
from accounts.models import User
from .models import LegalEntity,StockBalance,StockMovement,COMPANY_TAX_ID
from . import stock

@skipUnless(connection.vendor=='postgresql','Проверка блокировок требует PostgreSQL')
class StockConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.leaders=[User.objects.create_user(f'+7000000000{i}',secrets.token_urlsafe(24),leader_slot=i,full_name='ТЕСТ',must_change_password=False) for i in (1,2)]
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        self.a=stock.save_warehouse(self.leaders[0],None,None,{'name':'ТЕСТ А'})
        self.b=stock.save_warehouse(self.leaders[0],None,None,{'name':'ТЕСТ Б'})
        self.c=stock.save_warehouse(self.leaders[0],None,None,{'name':'ТЕСТ В'})
        self.item=stock.save_item(self.leaders[0],None,None,dict(sku='0001',name='ТЕСТ материал',kind='material',unit='kg',method='',diameter_mm=None,length_mm=None,wall_mm=None,archived=False,coupling=None))
    def race(self,operation):
        barrier=threading.Barrier(2)
        def run(i):
            close_old_connections()
            try:barrier.wait(timeout=10);operation(i);return 'saved'
            except ValidationError:return 'conflict'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(run,[0,1]))
        self.assertCountEqual(result,['saved','conflict'])
    def test_concurrent_transfers_cannot_spend_same_balance(self):
        stock.move(self.leaders[0],self.item.pk,'receipt',10,None,self.a.pk,timezone.localdate(),'ТЕСТ',uuid.uuid4())
        self.race(lambda i:stock.move(self.leaders[i],self.item.pk,'transfer',10,self.a.pk,[self.b.pk,self.c.pk][i],timezone.localdate(),'Перемещение',uuid.uuid4()))
        self.assertEqual(StockBalance.objects.get(warehouse=self.a,item=self.item).quantity,0)
        self.assertEqual(sum(StockBalance.objects.values_list('quantity',flat=True)),10)
        self.assertEqual(StockMovement.objects.count(),2)
    def test_same_token_cannot_be_posted_by_two_people(self):
        token=uuid.uuid4()
        self.race(lambda i:stock.move(self.leaders[i],self.item.pk,'receipt',10,None,self.a.pk,timezone.localdate(),'ТЕСТ',token))
        self.assertEqual(StockBalance.objects.get(warehouse=self.a,item=self.item).quantity,10);self.assertEqual(StockMovement.objects.count(),1)
    def test_repair_and_material_posting_on_same_job_do_not_deadlock(self):
        from unittest.mock import patch
        from repairs.models import Section,Job,Coupling
        from repairs.services import report_work
        from .models import ProjectObject
        obj=ProjectObject.objects.create(legal_entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID),name='ТЕСТ')
        section=Section.objects.create(project_object=obj,rnu='ТЕСТ',pipeline='ТЕСТ',name='ТЕСТ')
        job=Job.objects.create(section=section,method='p2',length_m=1,created_by=self.leaders[0]);coupling=Coupling.objects.create(project_object=obj,method='p2',length_mm=1000,wall_mm=8)
        stock.move(self.leaders[0],self.item.pk,'receipt',10,None,self.a.pk,timezone.localdate(),'ТЕСТ',uuid.uuid4())
        posting=threading.Event();repair_locked=threading.Event();real_post=stock.post_locked;real_lock=stock.lock
        def post(*args,**kwargs):
            if threading.current_thread().name.startswith('stock-post'):
                posting.set()
                if not repair_locked.wait(10):raise RuntimeError('Repair did not reach stock lock')
            return real_post(*args,**kwargs)
        def lock():
            if threading.current_thread().name.startswith('repair-post'):repair_locked.set()
            return real_lock()
        def material():
            close_old_connections()
            try:stock.move(self.leaders[0],self.item.pk,'consume',1,self.a.pk,None,timezone.localdate(),'Материал на работу',uuid.uuid4(),job.pk)
            finally:connections.close_all()
        def repair():
            close_old_connections()
            try:
                if not posting.wait(10):raise RuntimeError('Material did not start')
                report_work(self.leaders[1],job.pk,'result',{'token':uuid.uuid4(),'expected_version':1,'performed_date':timezone.localdate(),'outcome':'complete','method':'p2','length_m':1,'composition':'','note':'ТЕСТ','latitude':None,'longitude':None,'couplings':[coupling],'files':[]})
            finally:connections.close_all()
        with patch('projects.stock.post_locked',side_effect=post),patch('projects.stock.lock',side_effect=lock):
            with ThreadPoolExecutor(max_workers=1,thread_name_prefix='stock-post') as a,ThreadPoolExecutor(max_workers=1,thread_name_prefix='repair-post') as b:
                fa=a.submit(material);fb=b.submit(repair);fa.result(timeout=20);fb.result(timeout=20)
        job.refresh_from_db();self.assertEqual(job.state,'reported');self.assertEqual(StockBalance.objects.get(warehouse=self.a,item=self.item).quantity,9)
