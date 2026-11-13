import tempfile,uuid
from decimal import Decimal
from datetime import timedelta
from django.test import TestCase,override_settings
from django.core.exceptions import PermissionDenied,ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from projects import test_stock as fixture
from . import ddk,plans
from .models import Report,Attachment

class DDKTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    make_job=fixture.StockTests.make_job
    def setUp(self):
        self.job=self.make_job();self.job.method='grind';self.job.length_m=None;self.job.wall_mm=None;self.job.save()
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        override=override_settings(PRIVATE_DOCUMENT_ROOT=self.tmp.name);override.enable();self.addCleanup(override.disable)
    def data(self,**values):return {'token':uuid.uuid4(),'expected_version':self.job.version,'method':'p1','length_m':Decimal('3'),'composition':'1.5+1.5','wall_mm':Decimal('10'),'date':self.today,'reason':'ТЕСТ заключение ДДК','files':[],**values}
    def test_master_changes_grinding_to_coupling_without_completing_or_granting_admin(self):
        result=ddk.change(self.worker,self.job.pk,self.data());self.job.refresh_from_db()
        self.assertEqual((self.job.method,self.job.length_m,self.job.wall_mm),('p1',3,10))
        self.assertEqual(self.job.state,'planned');self.assertFalse(self.job.installed_couplings.exists())
        self.assertEqual(result.context['previous_plan']['method'],'grind');self.assertTrue(result.context['ddk'])
        self.assertFalse(self.worker.grants.filter(permission='repairs.prepare').exists())
        with self.assertRaises(PermissionDenied):plans.revise(self.worker,self.job.pk,self.job.version,{k:getattr(self.job,k) for k in plans.FIELDS},'ТЕСТ')
    def test_retries_are_idempotent_and_modified_retries_refused(self):
        data=self.data();first=ddk.change(self.worker,self.job.pk,data)
        self.assertEqual(ddk.change(self.worker,self.job.pk,data).pk,first.pk)
        with self.assertRaises(ValidationError):ddk.change(self.worker,self.job.pk,{**data,'reason':'другое основание'})
        self.assertEqual(Report.objects.filter(kind='plan_change').count(),1)
    def test_closed_stale_and_unrelated_requests_are_denied(self):
        with self.assertRaises(ValidationError):ddk.change(self.worker,self.job.pk,self.data(expected_version=999))
        self.job.state='confirmed';self.job.save()
        with self.assertRaises(ValidationError):ddk.change(self.worker,self.job.pk,self.data())
        with self.assertRaises(PermissionDenied):ddk.change(self.supply,self.job.pk,self.data())
    def test_dimensions_date_and_basis_are_validated_on_service(self):
        for values in ({'method':'bad'},{'wall_mm':None},{'reason':''},{'date':self.today+timedelta(days=1)},{'composition':'1+1'}):
            with self.assertRaises(ValidationError):ddk.change(self.worker,self.job.pk,self.data(**values))
        self.assertFalse(Report.objects.exists())
    def test_evidence_is_attached_and_form_supports_validated_uploads(self):
        self.client.force_login(self.worker)
        data=self.data(files=[SimpleUploadedFile('ddk.pdf',b'%PDF-TEST')])
        response=self.client.post(reverse('repair_ddk',args=[self.job.pk]),data)
        self.assertEqual(response.status_code,302);self.assertEqual(Attachment.objects.count(),1)
        self.assertEqual(Attachment.objects.get().report.context['previous_plan']['method'],'grind')
    def test_completion_entry_and_bot_point_to_ddk_form(self):
        self.client.force_login(self.worker)
        response=self.client.get(reverse('repair_report',args=[self.job.pk,'repaired']))
        self.assertContains(response,reverse('repair_ddk',args=[self.job.pk]))
