from unittest.mock import patch
import tempfile,uuid
from django.test import TestCase,override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from projects import test_release624 as fixture,rentals,rental_settlements as ledger,rental_files as files
from . import tests as config,worker
from .models import Binding,Inbox,Outbox


@override_settings(**config.CONFIG)
class RentalDocumentsBotTests(TestCase):
    setUpTestData=classmethod(fixture.Release624Tests.setUpTestData.__func__)
    crew=fixture.Release624Tests.crew
    rental=fixture.Release624Tests.rental

    def setUp(self):
        root=tempfile.TemporaryDirectory();self.addCleanup(root.cleanup)
        settings=override_settings(PRIVATE_DOCUMENT_ROOT=root.name);settings.enable();self.addCleanup(settings.disable)
        self.hire=self.rental();rentals.save_day(self.worker,self.hire.pk,self.today,'work')
        self.invoice=ledger.create_invoice(self.leader,self.hire.pk,self.today,self.today,self.today,'ТЕСТ',1000,uuid.uuid4())
        self.binding=Binding.objects.create(user=self.leader,max_user=101,chat=201,session_version=self.leader.session_version)

    def send(self,text='',photos=None):
        event=Inbox.objects.create(key=uuid.uuid4().hex,max_user=101,chat=201,payload={'kind':'message_created','text':text,'photos':photos or []})
        worker.process(event.pk);event.refresh_from_db();self.assertEqual(event.state,'done')
        self.binding.refresh_from_db();return Outbox.objects.get(key__startswith=f'in:{event.pk}:')

    def test_native_invoice_upload_preview_confirm_and_cancel(self):
        self.send(f'rentfile:start:{self.hire.pk}:{self.invoice.pk}:act')
        with patch('maxbot.transport.download_file',return_value=SimpleUploadedFile('test.pdf',b'%PDF-1.4\nTEST')):
            self.send(photos=[{'type':'file','url':'https://synthetic.invalid/test','name':'test.pdf'}])
        row=files.RentalInvoiceFile.objects.get();self.assertEqual(row.state,'pending')
        self.send(f'rentfile:confirm:{self.hire.pk}:{row.pk}');row.refresh_from_db();self.assertEqual(row.state,'available')
        listing=self.send(f'rentfile:list:{self.hire.pk}:{self.invoice.pk}:0')
        self.assertIn('test.pdf',str(listing.body))
        self.send(f'rentfile:start:{self.hire.pk}:{self.invoice.pk}:invoice');self.send('rentfile:cancel')
        self.assertEqual(files.RentalInvoiceFile.objects.count(),1)
