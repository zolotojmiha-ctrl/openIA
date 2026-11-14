import tempfile
from datetime import timedelta
from django.core.exceptions import PermissionDenied,ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase,override_settings
from django.utils import timezone
from django.utils.datastructures import MultiValueDict
from repairs.models import Job,Report,Attachment
from . import test_stock as fixtures
from .max_prototype import Prototype

class MaxPrototypeTests(TestCase):
    setUpTestData=classmethod(fixtures.StockTests.setUpTestData.__func__)
    def setUp(self):
        self.bot=Prototype()
        self.job=Job.objects.create(contract=self.contract,section=self.section,method='grind',assigned_to=self.worker,created_by=self.leader)
        self.link()
    def link(self):
        code=self.bot.issue_link(self.worker)
        request=self.bot.request_link(code,101,201)
        self.bot.approve_link(self.worker,request)
    def values(self,**extra):
        return {'performed_date':str(timezone.localdate()),'outcome':'complete','method':'grind','actual_coating_length_m':'2.4','note':'ТЕСТ Результат',**extra}
    def prepare(self,**kwargs):
        return self.bot.prepare(101,201,self.job.pk,'result',self.values(),**kwargs)
    def test_link_requires_approval_and_code_cannot_be_replayed(self):
        b=Prototype();code=b.issue_link(self.worker);request=b.request_link(code,102,202)
        with self.assertRaises(PermissionDenied):b.jobs(102,202)
        with self.assertRaises(ValidationError):b.request_link(code,102,202)
        with self.assertRaises(PermissionDenied):b.approve_link(self.supply,request)
        b.approve_link(self.worker,request);self.assertEqual(len(b.jobs(102,202)),1)
    def test_only_foremen_can_link(self):
        with self.assertRaises(PermissionDenied):self.bot.issue_link(self.leader)
    def test_prepare_creates_no_report_and_preserves_job_state(self):
        self.prepare();self.assertEqual(Report.objects.count(),0)
        self.job.refresh_from_db();self.assertEqual(self.job.state,'planned')
    def test_confirm_uses_existing_reporting_and_repeat_is_idempotent(self):
        row=self.prepare()
        with self.assertRaises(ValidationError):self.bot.confirm(101,201,row['confirmation_id'],confirmed=False)
        first=self.bot.confirm(101,201,row['confirmation_id'],confirmed=True)
        second=self.bot.confirm(101,201,row['confirmation_id'],confirmed=True)
        self.assertEqual(first.pk,second.pk);self.assertEqual(Report.objects.count(),1)
        self.job.refresh_from_db();self.assertEqual(self.job.state,'reported');self.assertEqual(first.author,self.worker)
    def test_other_chat_cannot_submit_draft(self):
        row=self.prepare()
        with self.assertRaises(PermissionDenied):self.bot.confirm(101,202,row['confirmation_id'],confirmed=True)
    def test_revoke_stops_confirm(self):
        row=self.prepare();self.bot.revoke(self.worker)
        with self.assertRaises(PermissionDenied):self.bot.confirm(101,201,row['confirmation_id'],confirmed=True)
        self.assertFalse(Report.objects.exists())
    def test_session_change_stops_link(self):
        type(self.worker).objects.filter(pk=self.worker.pk).update(session_version=self.worker.session_version+1)
        with self.assertRaises(PermissionDenied):self.bot.jobs(101,201)
    def test_reassigned_job_stops_old_draft(self):
        row=self.prepare();Job.objects.filter(pk=self.job.pk).update(assigned_to=self.supply)
        with self.assertRaises(PermissionDenied):self.bot.confirm(101,201,row['confirmation_id'],confirmed=True)
    def test_version_conflict_preserves_job_and_report(self):
        row=self.prepare();Job.objects.filter(pk=self.job.pk).update(version=self.job.version+1)
        with self.assertRaises(ValidationError):self.bot.confirm(101,201,row['confirmation_id'],confirmed=True)
        self.assertFalse(Report.objects.exists())
    def test_expired_draft_is_not_saved(self):
        row=self.prepare()
        import uuid
        self.bot.drafts[uuid.UUID(row['confirmation_id'])].expires=timezone.now()-timedelta(seconds=1)
        with self.assertRaises(ValidationError):self.bot.confirm(101,201,row['confirmation_id'],confirmed=True)
    def test_problem_is_saved_through_standard_rules(self):
        row=self.bot.prepare(101,201,self.job.pk,'problem',self.values(problem='water'))
        report=self.bot.confirm(101,201,row['confirmation_id'],confirmed=True)
        self.job.refresh_from_db();self.assertEqual(self.job.problem,'water');self.assertEqual(report.author,self.worker)
    def test_photo_uses_standard_validation_and_private_storage(self):
        import io
        from PIL import Image
        output=io.BytesIO();Image.new('RGB',(2,2),'white').save(output,format='PNG')
        photo=SimpleUploadedFile('demo.png',output.getvalue(),content_type='image/png')
        with tempfile.TemporaryDirectory() as root,override_settings(PRIVATE_DOCUMENT_ROOT=root):
            row=self.prepare(files=MultiValueDict({'files':[photo]}));self.assertEqual(row['files'],1)
            self.bot.confirm(101,201,row['confirmation_id'],confirmed=True)
            self.assertEqual(Attachment.objects.count(),1)

    def test_relink_cannot_resurrect_old_draft(self):
        row=self.prepare();self.bot.revoke(self.worker);self.link()
        with self.assertRaises(PermissionDenied):self.bot.confirm(101,201,row['confirmation_id'],confirmed=True)
    def test_invalid_photo_cannot_reach_reporting(self):
        with self.assertRaises(ValidationError):
            self.prepare(files=MultiValueDict({'files':[SimpleUploadedFile('bad.png',b'not an image',content_type='image/png')]}))
        self.assertFalse(Report.objects.exists())
    def test_link_expiry_blocks_pending_request(self):
        code=self.bot.issue_link(self.worker)
        old=self.bot.codes[code];self.bot.codes[code]=(old[0],old[1],timezone.now()-timedelta(seconds=1))
        with self.assertRaises(ValidationError):self.bot.request_link(code,102,202)
    def test_inaccessible_job_cannot_be_selected(self):
        other=Job.objects.create(contract=self.contract,section=self.section,method='grind',assigned_to=self.supply,created_by=self.leader)
        with self.assertRaises(PermissionDenied):self.bot.prepare(101,201,other.pk,'result',self.values())
