from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
import tempfile
from django.core.exceptions import ValidationError
from django.db import connection,connections,close_old_connections
from django.test import TransactionTestCase,override_settings
from projects import test_stock as fixture
from projects.models import LegalEntity,COMPANY_TAX_ID
from .test_completed_import import plan_for,upload
from . import completed_import as service
from .models import Job,Defect


@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL locks')
class CompletedConcurrencyTests(TransactionTestCase):
    def setUp(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        self.contract.number='ТЕСТ ТЗС-1731-2025';self.contract.status='active';self.contract.save()
        root=tempfile.TemporaryDirectory();self.addCleanup(root.cleanup)
        cfg=override_settings(PRIVATE_DOCUMENT_ROOT=root.name);cfg.enable();self.addCleanup(cfg.disable)
        self.plan=plan_for(self.contract,self.obj)
    def parallel(self,callbacks):
        barrier=Barrier(len(callbacks))
        def run(fn):
            close_old_connections()
            try:barrier.wait(timeout=10);return fn()
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=len(callbacks)) as pool:return list(pool.map(run,callbacks))
    def test_same_confirmation_is_once(self):
        batch=service.prepare(self.leader,self.contract.pk,upload(self.plan))
        rows=self.parallel([lambda:service.apply(self.leader,batch.pk,batch.payload['digest']) for _ in range(2)])
        self.assertEqual(rows[0],rows[1]);self.assertEqual(Job.objects.count(),2);self.assertEqual(Defect.objects.count(),2)
    def test_two_previews_cannot_both_insert_same_missing_defects(self):
        batches=[service.prepare(self.leader,self.contract.pk,upload(self.plan)) for _ in range(2)]
        def apply(batch):
            try:service.apply(self.leader,batch.pk,batch.payload['digest']);return 'applied'
            except ValidationError:return 'stale'
        self.assertEqual(sorted(self.parallel([lambda b=b:apply(b) for b in batches])),['applied','stale'])
        self.assertEqual(Job.objects.count(),2);self.contract.refresh_from_db();self.assertEqual(self.contract.status,'active')
