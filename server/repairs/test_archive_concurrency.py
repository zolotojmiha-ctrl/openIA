"""Actual PostgreSQL locking: LC/MAX share actor -> contract -> preview order."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
import tempfile
from django.db import connection,connections,close_old_connections
from django.test import TransactionTestCase,override_settings
from projects import test_stock as fixture
from projects.models import LegalEntity,COMPANY_TAX_ID,Contract,ContractObject
from accounts.models import User
from .test_archive_import import plan_for,upload
from . import archive_import as service
from .models import Job,Defect

@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL locks')
class ArchiveConcurrencyTests(TransactionTestCase):
    def setUp(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        root=tempfile.TemporaryDirectory();self.addCleanup(root.cleanup)
        config=override_settings(PRIVATE_DOCUMENT_ROOT=root.name);config.enable();self.addCleanup(config.disable)
        self.archive=Contract.objects.create(legal_entity=self.company,number=service.TITLE,subject='ТЕСТ',work_kinds=['repairs'],status='planned')
        ContractObject.objects.create(contract=self.archive,project_object=self.obj)
        self.plan=plan_for(self.archive,self.obj)
    def parallel(self,callbacks):
        barrier=Barrier(len(callbacks))
        def run(fn):
            close_old_connections()
            try:barrier.wait(timeout=10);return fn()
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=len(callbacks)) as pool:return list(pool.map(run,callbacks))
    def test_simultaneous_same_confirmation_inserts_once(self):
        batch=service.prepare(self.leader,self.archive.pk,upload(self.plan))
        rows=self.parallel([lambda:service.apply(self.leader,batch.pk,batch.payload['digest']) for _ in range(2)])
        self.assertEqual(rows[0],rows[1]);self.assertEqual(Job.objects.filter(contract=self.archive).count(),2)
        self.assertEqual(Defect.objects.filter(contract=self.archive).count(),3)
    def test_two_leaders_two_previews_same_target_do_not_duplicate(self):
        other=User.objects.create_user('+70000990077',full_name='ТЕСТ второй руководитель',leader_slot=2,must_change_password=False)
        batches=[service.prepare(u,self.archive.pk,upload(self.plan)) for u in (self.leader,other)]
        rows=self.parallel([lambda u=u,b=b:service.apply(u,b.pk,b.payload['digest']) for u,b in zip((self.leader,other),batches)])
        self.assertEqual(rows[0],rows[1]);self.assertEqual(Job.objects.filter(contract=self.archive).count(),2)
        self.assertEqual(Defect.objects.filter(contract=self.archive).count(),3)
