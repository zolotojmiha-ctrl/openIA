import io,uuid,tempfile
from datetime import timedelta
from PIL import Image
from django.test import TestCase,override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from projects import test_stock as fixture
from projects.models import StockMovement,RepairConsumption
from .progress import ProgressForm
from .services import report_work
from .models import Report,Attachment


def photo():
    f=io.BytesIO();Image.new('RGB',(20,20),'blue').save(f,'PNG')
    return SimpleUploadedFile('TEST.png',f.getvalue(),content_type='image/png')


class ProgressTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    make_job=fixture.StockTests.make_job
    def setUp(self):
        self.root=tempfile.TemporaryDirectory();self.addCleanup(self.root.cleanup)
        setting=override_settings(PRIVATE_DOCUMENT_ROOT=self.root.name);setting.enable();self.addCleanup(setting.disable)
    def data(self,job,**changes):
        form=ProgressForm({'token':uuid.uuid4(),'expected_version':job.version,'performed_date':self.today,'stages':['excavation','ddk'],'note':'TEST',**changes}, {'files':[photo()]},job=job,user=self.worker)
        self.assertTrue(form.is_valid(),form.errors);return form.cleaned_data
    def test_four_days_append_evidence_without_installation_or_consumption(self):
        job=self.make_job();count=StockMovement.objects.count()
        for day in range(4):
            job.refresh_from_db()
            report=report_work(self.worker,job.pk,'note',self.data(job,performed_date=self.today-timedelta(days=3-day)))
            self.assertEqual(report.context['progress']['stages'],['excavation','ddk'])
        job.refresh_from_db();self.assertEqual(job.state,'progress')
        self.assertEqual(job.version,2)
        self.assertEqual(job.reports.count(),4);self.assertEqual(Attachment.objects.count(),4)
        self.assertFalse(job.installed_couplings.exists());self.assertFalse(RepairConsumption.objects.exists());self.assertEqual(StockMovement.objects.count(),count)
        self.client.force_login(self.leader)
        response=self.client.get(reverse('repair_detail',args=[job.pk]));self.assertContains(response,'Ход работ');self.assertContains(response,'Вскрыли котлован, Провели ДДК')
    def test_three_jobs_same_day_and_retry_stay_separate(self):
        jobs=[self.make_job() for _ in range(3)]
        for job in jobs:
            data=self.data(job);report=report_work(self.worker,job.pk,'note',data)
            self.assertEqual(report_work(self.worker,job.pk,'note',data).pk,report.pk)
        self.assertEqual(Report.objects.count(),3)
        for job in jobs:self.assertEqual(Attachment.objects.filter(report__job=job).count(),1)
    def test_empty_or_invalid_images_and_future_date_are_rejected(self):
        job=self.make_job();payload={'token':uuid.uuid4(),'expected_version':job.version,'performed_date':self.today}
        for file in (None,SimpleUploadedFile('fake.png',b'not a picture')):
            form=ProgressForm(payload,{'files':[file]} if file else None,job=job,user=self.worker)
            self.assertFalse(form.is_valid());self.assertIn('files',form.errors)
        form=ProgressForm(dict(payload,performed_date=self.today+timedelta(days=1)),{'files':[photo()]},job=job,user=self.worker)
        self.assertFalse(form.is_valid());self.assertIn('performed_date',form.errors)
        self.assertFalse(Report.objects.exists())
    def test_no_stage_is_required_and_completed_job_is_protected(self):
        job=self.make_job();data=self.data(job,stages=[],note='')
        self.assertEqual(data['progress'],{'stages':[]})
        job.state='reported';job.save()
        with self.assertRaises(ValidationError):report_work(self.worker,job.pk,'note',data)
        self.assertFalse(Report.objects.exists())
    def test_web_submission_and_permissions(self):
        job=self.make_job();self.client.force_login(self.worker)
        url=reverse('repair_report',args=[job.pk,'progress'])
        self.assertContains(self.client.get(url),'Сохранить фотоотчёт')
        response=self.client.post(url,{'token':uuid.uuid4(),'expected_version':job.version,'performed_date':self.today,'files':photo(),'stages':['surface']})
        self.assertEqual(response.status_code,302);self.assertEqual(Report.objects.get().context['progress']['stages'],['surface'])
        job.assigned_to=self.leader;job.save()
        self.assertEqual(self.client.get(url).status_code,404)


    def test_archived_object_cannot_receive_new_progress(self):
        job=self.make_job();data=self.data(job)
        self.obj.status='archived';self.obj.save()
        with self.assertRaises(ValidationError):report_work(self.worker,job.pk,'note',data)
        self.assertFalse(Report.objects.exists())
