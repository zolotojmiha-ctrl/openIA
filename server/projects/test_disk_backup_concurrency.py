from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
import uuid
from django.db import connection,connections
from django.test import TransactionTestCase
from django.core.exceptions import ValidationError
from . import disk_backups,test_stock as fixture
from . import test_disk_backups as backup_tests
from .models import LegalEntity,COMPANY_TAX_ID,DiskBackupJob

@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL row locks')
class BackupConcurrency(TransactionTestCase):
    def test_two_requests_and_workers_create_and_claim_one_job(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        backup_tests.DiskBackupTests.configure(self)
        def race(operation):
            gate=Barrier(2)
            def work():
                connections.close_all()
                try:
                    gate.wait(timeout=10)
                    try:return operation()
                    except ValidationError:return None
                finally:connections.close_all()
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(work) for _ in range(2)]
                return [f.result(timeout=30) for f in futures]
        race(lambda:disk_backups.enqueue(self.leader,uuid.uuid4()))
        self.assertEqual(DiskBackupJob.objects.count(),1)
        claimed=race(disk_backups.claim)
        self.assertEqual(len([pk for pk in claimed if pk]),1)
        self.assertEqual(DiskBackupJob.objects.get().state,'running')
