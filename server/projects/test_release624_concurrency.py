import secrets,threading,uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest import skipUnless
from django.db import connection,connections,close_old_connections
from django.test import TransactionTestCase
from django.core.exceptions import ValidationError
from accounts.models import User
from . import test_stock as base,test_release624 as release,rentals,repair_materials,stock
from .models import LegalEntity,COMPANY_TAX_ID,RepairConsumption,StockMovement

@skipUnless(connection.vendor=='postgresql','Проверка блокировок требует PostgreSQL')
class Release624ConcurrencyTests(TransactionTestCase):
    wh=base.StockTests.wh;data=base.StockTests.data;item=base.StockTests.item;move=base.StockTests.move;crew=base.StockTests.crew
    make_job=base.StockTests.make_job;concrete=base.StockTests.concrete;balance=base.StockTests.balance
    rental=release.Release624Tests.rental;profile=release.Release624Tests.profile
    def setUp(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        base.StockTests.setUpTestData.__func__(type(self));self.section.diameter_mm=720;self.section.save()
        self.second=User.objects.create_user('+70000000004',secrets.token_urlsafe(24),leader_slot=2,full_name='ТЕСТ Второй руководитель',must_change_password=False)
    def race(self,operation):
        barrier=threading.Barrier(2)
        def run(i):
            close_old_connections()
            try:barrier.wait(timeout=15);operation(i);return 'saved'
            except ValidationError:return 'conflict'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(run,[0,1]))
    def test_same_rental_day_parallel_click_is_one_record(self):
        row=self.rental();actors=[self.leader,self.second]
        self.assertEqual(self.race(lambda i:rentals.save_day(actors[i],row.pk,self.today,'work')),['saved','saved'])
        self.assertEqual(row.days.count(),1);self.assertEqual(row.days.get().revisions.count(),1);self.assertEqual(rentals.totals(row)['charged'],1000)
    def test_different_parallel_day_results_require_correction(self):
        row=self.rental();actors=[self.leader,self.second]
        self.assertCountEqual(self.race(lambda i:rentals.save_day(actors[i],row.pk,self.today,['work','idle'][i])),['saved','conflict'])
        self.assertEqual(row.days.count(),1)
    def test_payment_retry_by_two_leaders_is_not_double_paid(self):
        row=self.rental();actors=[self.leader,self.second];token=uuid.uuid4()
        self.assertEqual(self.race(lambda i:rentals.payment(actors[i],row.pk,self.today,Decimal(100),'ТЕСТ аванс',token)),['saved','saved'])
        self.assertEqual(row.payments.count(),1);self.assertEqual(rentals.totals(row)['paid'],100)
    def test_two_repairs_cannot_spend_same_consumables(self):
        from repairs.models import Coupling
        from repairs.services import report_work
        p,wh,items=self.profile();stock.move(self.leader,items['tape'].pk,'loss',60,wh.pk,None,self.today,'ТЕСТ лимит',uuid.uuid4())
        jobs=[];couplings=[]
        for index in range(2):
            j=self.make_job();c=Coupling.objects.create(project_object=self.obj,method='p2',length_mm=1000,wall_mm=8,serial='ТЕСТ-'+str(index))
            item=self.item(sku='coupling-'+str(index),kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8,coupling=c);self.move(item,q=1,dst=wh);jobs.append(j);couplings.append(c)
        actors=[self.leader,self.second]
        def report(i):return report_work(actors[i],jobs[i].pk,'result',{'token':uuid.uuid4(),'expected_version':1,'performed_date':self.today,'outcome':'complete','method':'p2','length_m':1,'composition':'','latitude':None,'longitude':None,'couplings':[couplings[i]],'files':[]})
        self.assertEqual(self.race(report),['saved','saved'])
        self.assertEqual(RepairConsumption.objects.filter(state='posted').count(),1);self.assertEqual(RepairConsumption.objects.filter(state='pending').count(),1)
        self.assertEqual(StockMovement.objects.filter(kind='consume',item__unit='kg').count(),3)
        self.assertEqual(self.balance(wh,items['tape']),Decimal('7.443'))
