import tempfile,threading
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from django.test import TransactionTestCase,override_settings
from django.db import connection,connections,transaction
from documents.storage import store_photo_file as store_file,validate_upload,discard_file,private_path
from .models import Attachment,Report
from . import test_progress as fixture


@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL transaction/advisory lock semantics')
class PhotoConcurrencyTests(TransactionTestCase):
    def setUp(self):
        from projects.models import LegalEntity,COMPANY_TAX_ID
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'SYNTHETIC'})
        fixture.ProgressTests.setUpTestData.__func__(type(self))
        self.root=tempfile.TemporaryDirectory();self.addCleanup(self.root.cleanup)
        settings=override_settings(PRIVATE_DOCUMENT_ROOT=self.root.name);settings.enable();self.addCleanup(settings.disable)
        self.job=fixture.ProgressTests.make_job(self)
        self.reports=[Report.objects.create(job=self.job,author=self.worker,kind='note',performed_date=self.today) for _ in range(2)]
        self.data=validate_upload(fixture.photo())

    def write(self,index):
        try:
            with transaction.atomic():
                key=store_file(self.data)
                Attachment.objects.create(report_id=self.reports[index].pk,storage_key=key,**{k:self.data[k] for k in ('name','size','sha256','content_type')})
                return key
        finally:connections.close_all()

    def test_concurrent_identical_uploads_share_one_physical_file(self):
        barrier=threading.Barrier(2)
        def write(index):barrier.wait(timeout=5);return self.write(index)
        with ThreadPoolExecutor(max_workers=2) as pool:keys=list(pool.map(write,range(2)))
        self.assertEqual(keys[0],keys[1]);self.assertEqual(Attachment.objects.count(),2)
        self.assertTrue(private_path(keys[0]).exists())

    def test_cleanup_waits_for_pending_reuse_reference(self):
        key=self.write(0);original=Attachment.objects.first();reused=threading.Event();commit=threading.Event()
        def reference():
            try:
                with transaction.atomic():
                    actual=store_file(self.data);reused.set()
                    if not commit.wait(5):raise AssertionError('timed out waiting for test commit')
                    Attachment.objects.create(report_id=self.reports[1].pk,storage_key=actual,**{k:self.data[k] for k in ('name','size','sha256','content_type')})
            finally:connections.close_all()
        def clean():
            try:return discard_file(key)
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            writer=pool.submit(reference);self.assertTrue(reused.wait(5))
            Attachment.objects.filter(pk=original.pk).update(storage_key='')
            cleaner=pool.submit(clean);commit.set();writer.result(timeout=10)
            self.assertFalse(cleaner.result(timeout=10))
        self.assertTrue(private_path(key).exists())
