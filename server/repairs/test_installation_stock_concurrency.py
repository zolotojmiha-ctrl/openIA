from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from django.db import connection,connections,close_old_connections
from django.test import TransactionTestCase
from django.core.exceptions import ValidationError
from projects.models import LegalEntity,COMPANY_TAX_ID,StockMovement
from projects import test_stock as fixtures
from accounts.models import User
from .test_installation_stock import InstallationStockTests
from . import stock_reconciliation as service
from .models import Coupling,CouplingUse,Report
from .services import report_work


@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL locks')
class InstallationStockConcurrencyTests(TransactionTestCase):
    wh=fixtures.StockTests.wh;data=fixtures.StockTests.data;make_job=fixtures.StockTests.make_job
    balance=fixtures.StockTests.balance;receive=InstallationStockTests.receive;payload=InstallationStockTests.payload

    def setUp(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixtures.StockTests.setUpTestData.__func__(type(self))
        self.job=self.make_job();self.job.method='p1';self.job.length_m=2;self.job.wall_mm=10;self.job.save()
        self.warehouse=self.wh(project_object=self.obj);self.item=self.receive()[0]

    def parallel(self,functions):
        barrier=Barrier(len(functions))
        def run(fn):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:return fn()
                except ValidationError:return False
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=len(functions)) as pool:return list(pool.map(run,functions))

    def test_same_report_retry_from_two_requests_posts_once(self):
        data=self.payload()
        self.parallel([lambda:report_work(self.worker,self.job.pk,'result',data),lambda:report_work(self.worker,self.job.pk,'result',data)])
        self.assertEqual(Report.objects.count(),1)
        self.assertEqual(StockMovement.objects.filter(kind='consume').count(),1)
        self.assertEqual(self.balance(self.warehouse,self.item),0)

    def test_two_jobs_competing_for_last_free_item_only_one_succeeds(self):
        other=self.make_job();other.method='p1';other.length_m=2;other.wall_mm=10;other.save()
        a=self.payload(serial='ТЕСТ А');b=self.payload(serial='ТЕСТ Б')
        result=self.parallel([lambda:report_work(self.worker,self.job.pk,'result',a),lambda:report_work(self.worker,other.pk,'result',b)])
        self.assertEqual(sum(bool(r) for r in result),1)
        self.assertEqual(StockMovement.objects.filter(kind='consume').count(),1)
        self.assertEqual(Report.objects.count(),1)

    def test_simultaneous_reconciliation_confirmation_posts_once(self):
        self.job.state='reported';self.job.save()
        coupling=Coupling.objects.create(project_object=self.obj,method='p1',length_mm=2000,wall_mm=10,serial='ТЕСТ ФАКТ',source={'reported_without_stock':True})
        report=Report.objects.create(job=self.job,author=self.worker,kind='complete',performed_date=self.today)
        CouplingUse.objects.create(job=self.job,coupling=coupling,report=report)
        _,token=service.preview(self.leader,self.job.pk,{})
        self.parallel([lambda:service.confirm(self.leader,token),lambda:service.confirm(self.leader,token)])
        self.assertEqual(StockMovement.objects.filter(kind='consume').count(),1)
        self.assertEqual(Report.objects.count(),1)
