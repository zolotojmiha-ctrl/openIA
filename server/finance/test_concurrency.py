import secrets
import tempfile
import shutil
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import date
from uuid import uuid4
from django.db import close_old_connections,connections
from django.test import TransactionTestCase,Client,override_settings,skipUnlessDBFeature
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from accounts.models import User
from projects.models import LegalEntity,COMPANY_TAX_ID
from documents.models import Document
from documents.services import make_version
from documents.storage import validate_upload
from .models import FinancialScope,CashReceipt,PaymentAllocation
from .services import total

class ConcurrentDataIntegrity(TransactionTestCase):
    def setUp(self):
        p=secrets.token_urlsafe(24)
        self.first=User.objects.create_user('+70000000001',p,full_name='ТЕСТ Руководитель 1',leader_slot=1,must_change_password=False)
        self.second=User.objects.create_user('+70000000002',p,full_name='ТЕСТ Руководитель 2',leader_slot=2,must_change_password=False)
        self.company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ Компания'})[0]
    @skipUnlessDBFeature('has_select_for_update')
    def test_two_allocations_cannot_overspend_one_receipt(self):
        scope=FinancialScope.objects.create(legal_entity=self.company,title='ТЕСТ конкурентный учёт',created_by=self.first)
        receipt=CashReceipt.objects.create(legal_entity=self.company,token=uuid4(),date=date.today(),amount=1000,source='ТЕСТ',reference='ТЕСТ',created_by=self.first)
        barrier=Barrier(2)
        def allocate(user):
            close_old_connections()
            try:
                client=Client();client.force_login(user);barrier.wait(timeout=10)
                return client.post(reverse('finance_allocate',args=[receipt.pk]),{'operation_token':uuid4(),'scope':scope.pk,'purpose':'advance','amount':700}).status_code
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(allocate,u) for u in [self.first,self.second]]
            statuses=[f.result(timeout=20) for f in futures]
        self.assertEqual(sorted(statuses),[200,302]);self.assertEqual(total(receipt.allocations),700);self.assertEqual(PaymentAllocation.objects.count(),1)
    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_file_replacements_preserve_the_winning_version(self):
        with tempfile.TemporaryDirectory(prefix='armada-concurrent-files-') as root,override_settings(PRIVATE_DOCUMENT_ROOT=root):
            doc=Document.objects.create(title='ТЕСТ конкурентный файл',section='general',created_by=self.first,legal_entity=self.company)
            make_version(doc,self.first,validate_upload(SimpleUploadedFile('test.txt',b'first version')))
            revision=doc.revision;barrier=Barrier(2)
            def replace(user):
                close_old_connections()
                try:
                    client=Client();client.force_login(user);barrier.wait(timeout=10)
                    return client.post(reverse('document_replace',args=[doc.pk]),{'expected_revision':revision,'file':SimpleUploadedFile('test.txt',b'new version'),'comment':'ТЕСТ одновременная правка'}).status_code
                finally:connections.close_all()
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(replace,u) for u in [self.first,self.second]]
                statuses=[f.result(timeout=20) for f in futures]
            self.assertEqual(sorted(statuses),[200,302]);self.assertEqual(doc.versions.count(),2)
