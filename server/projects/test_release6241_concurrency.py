import secrets,threading,uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest import skipUnless
from unittest.mock import patch
from . import stock
from django.db import connection,connections,close_old_connections
from django.test import TransactionTestCase
from django.core.exceptions import ValidationError
from . import test_stock as base,test_release6241 as features
from .models import LegalEntity,COMPANY_TAX_ID,StockItem
from repairs import plans
from repairs.services import report_work
from repairs.models import CouplingUse,Report

@skipUnless(connection.vendor=='postgresql','Требуется PostgreSQL')
class Release6241ConcurrencyTests(TransactionTestCase):
    wh=base.StockTests.wh;data=base.StockTests.data;item=base.StockTests.item;move=base.StockTests.move;crew=base.StockTests.crew;make_job=base.StockTests.make_job;balance=base.StockTests.balance;reserved=features.Release6241Tests.reserved
    def compete(self,winner):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'});base.StockTests.setUpTestData.__func__(type(self));self.section.diameter_mm=720;self.section.save()
        job,wh,item=self.reserved();barrier=threading.Barrier(2);held=threading.Event();waiting=threading.Event();identity=threading.local();real_lock=stock.lock
        def ordered_lock():
            if identity.index==winner:
                result=real_lock();held.set()
                if not waiting.wait(10):raise RuntimeError('Second actor did not arrive')
                return result
            waiting.set()
            if not held.wait(10):raise RuntimeError('First actor did not acquire stock lock')
            return real_lock()
        def run(index):
            close_old_connections();identity.index=index
            try:
                barrier.wait(timeout=15)
                if index==0:
                    values={k:getattr(job,k) for k in plans.FIELDS};values['length_m']=Decimal('1.5');plans.revise(self.leader,job.pk,1,values,'ТЕСТ Обследование')
                else:report_work(self.worker,job.pk,'result',{'token':uuid.uuid4(),'expected_version':1,'performed_date':self.today,'outcome':'complete','method':'p2','length_m':1,'composition':'','note':'ТЕСТ Монтаж','latitude':None,'longitude':None,'couplings':[item.coupling],'files':[]})
                return 'saved'
            except ValidationError:return 'conflict'
            finally:connections.close_all()
        with patch('projects.stock.lock',side_effect=ordered_lock):
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[0,1]))
        self.assertCountEqual(results,['saved','conflict']);job.refresh_from_db();item.refresh_from_db()
        if job.length_m==Decimal('1.5'):
            self.assertFalse(CouplingUse.objects.filter(job=job).exists());self.assertEqual(self.balance(wh,item),1);self.assertTrue(Report.objects.filter(job=job,kind='plan_change').exists())
        else:self.assertEqual(job.state,'reported');self.assertEqual(self.balance(wh,item),0);self.assertTrue(CouplingUse.objects.filter(job=job).exists())

    def test_inspection_wins_before_installation_without_deadlock(self):self.compete(0)
    def test_installation_wins_before_inspection_without_deadlock(self):self.compete(1)
