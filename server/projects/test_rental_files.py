import tempfile,uuid
from datetime import timedelta
from django.test import TestCase,override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import PermissionDenied,ValidationError
from django.urls import reverse
from django.utils import timezone
from documents.storage import private_path
from . import test_release624 as fixture,rentals,rental_settlements as ledger,rental_files as files


class RentalInvoiceFileTests(TestCase):
    setUpTestData=classmethod(fixture.Release624Tests.setUpTestData.__func__)
    crew=fixture.Release624Tests.crew
    rental=fixture.Release624Tests.rental

    def setUp(self):
        root=tempfile.TemporaryDirectory();self.addCleanup(root.cleanup)
        settings=override_settings(PRIVATE_DOCUMENT_ROOT=root.name);settings.enable();self.addCleanup(settings.disable)
        self.hire=self.rental();rentals.save_day(self.worker,self.hire.pk,self.today,'work')
        self.invoice=ledger.create_invoice(self.leader,self.hire.pk,self.today,self.today,self.today,'ТЕСТ счёт',1000,uuid.uuid4())

    def upload(self):return SimpleUploadedFile('test-invoice.pdf',b'%PDF-1.4\nSYNTHETIC INVOICE\n%%EOF')

    def preview(self):return files.preview(self.leader,self.hire.pk,self.invoice.pk,'invoice',self.upload(),uuid.uuid4())

    def test_private_preview_confirm_and_download_with_live_rights(self):
        row=self.preview();url=reverse('rental_invoice_file',args=[self.hire.pk,row.pk])
        self.client.force_login(self.leader);self.assertEqual(self.client.get(url).status_code,404)
        for _ in range(2):files.confirm(self.leader,self.hire.pk,row.pk)
        response=self.client.get(url);self.assertEqual(response.status_code,200)
        self.assertEqual(b''.join(response.streaming_content),self.upload().read())
        self.assertIn('attachment',response['Content-Disposition'])
        self.client.force_login(self.worker);self.assertEqual(self.client.get(url).status_code,404)
        with self.assertRaises(PermissionDenied):files.confirm(self.worker,self.hire.pk,row.pk)

    def test_cancel_and_expired_upload_remove_bytes_without_changing_invoice(self):
        row=self.preview();path=private_path(row.storage_key)
        with self.captureOnCommitCallbacks(execute=True):files.cancel(self.leader,self.hire.pk,row.pk)
        self.assertFalse(path.exists());row.refresh_from_db();self.assertEqual(row.state,'cancelled')
        second=self.preview();path=private_path(second.storage_key)
        second.expires=timezone.now()-timedelta(seconds=1);second.save(update_fields=['expires'])
        with self.assertRaises(ValidationError):files.confirm(self.leader,self.hire.pk,second.pk)
        with self.captureOnCommitCallbacks(execute=True):self.assertEqual(files.cleanup_expired(),1)
        self.assertFalse(path.exists());self.assertEqual(ledger.invoice_balance(self.invoice)['charged'],1000)

    def test_void_invoice_and_revoked_session_cannot_confirm(self):
        row=self.preview();self.leader.session_version+=1;self.leader.save()
        with self.assertRaises(PermissionDenied):files.confirm(self.leader,self.hire.pk,row.pk)
        row=self.preview();ledger.void_invoice(self.leader,self.hire.pk,self.invoice.pk,'ТЕСТ отмена')
        with self.assertRaises(ValidationError):files.confirm(self.leader,self.hire.pk,row.pk)

    def test_lc_upload_requires_separate_confirmation(self):
        self.client.force_login(self.leader);url=reverse('rental_detail',args=[self.hire.pk])
        response=self.client.post(url,{'action':'file_preview','invoice_id':self.invoice.pk,'kind':'act','token':uuid.uuid4(),'upload':self.upload()})
        self.assertEqual(response.status_code,302)
        row=files.RentalInvoiceFile.objects.get();self.assertEqual(row.state,'pending')
        response=self.client.post(url,{'action':'file_confirm','file_id':row.pk})
        self.assertEqual(response.status_code,302);row.refresh_from_db();self.assertEqual(row.state,'available')
