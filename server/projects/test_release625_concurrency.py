import tempfile,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest import skipUnless
from django.db import connection,connections
from django.test import TransactionTestCase,override_settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from . import test_stock as base,agreements
from .models import LegalEntity,COMPANY_TAX_ID,CounterpartyAgreement
from documents.models import Document

@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL row locks')
class AgreementConcurrency625Tests(TransactionTestCase):
    def setUp(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'});base.StockTests.setUpTestData.__func__(type(self));self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);settings=override_settings(PRIVATE_DOCUMENT_ROOT=self.tmp.name);settings.enable();self.addCleanup(settings.disable)
    def data(self):return {'number':'ТЕСТ СОВМЕСТНО','kind':'equipment_rent','counterparty':'ТЕСТ','subject':'Аренда','expected_version':0,'customer_contracts':[str(self.contract.pk)],'project_objects':[str(self.obj.pk)]}
    def parallel(self,call):
        gate=Barrier(2)
        def run(n):
            connections.close_all()
            try:gate.wait(timeout=10);return call(n)
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(run,0);b=pool.submit(run,1);return [a.result(timeout=25),b.result(timeout=25)]
    def test_repeated_new_agreement_request_creates_one_file_and_one_agreement(self):
        data=self.data();data['submission_token']=str(uuid.uuid4())
        def call(n):return agreements.save(self.leader,None,data,uploads=[SimpleUploadedFile('ТЕСТ.txt',b'one immutable attachment')]).pk
        ids=self.parallel(call);self.assertEqual(ids[0],ids[1]);self.assertEqual(CounterpartyAgreement.objects.count(),1);self.assertEqual(Document.objects.count(),1);self.assertEqual(len([p for p in Path(self.tmp.name).rglob('*') if p.is_file()]),1)
    def test_stale_edit_does_not_add_losing_attachment_or_replace_links(self):
        row=agreements.save(self.leader,None,self.data());data=self.data();data['expected_version']=1
        def call(n):
            try:return agreements.save(self.leader,row.pk,dict(data,subject='ТЕСТ Редакция '+str(n)),uploads=[SimpleUploadedFile(str(n)+'.txt',b'private')]).version
            except ValidationError:return 'conflict'
        result=self.parallel(call);self.assertCountEqual(result,[2,'conflict']);row.refresh_from_db();self.assertEqual(row.documents.count(),1);self.assertEqual(row.customer_contracts.count(),1);self.assertEqual(Document.objects.count(),1)
