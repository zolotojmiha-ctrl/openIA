import os,base64,secrets,subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from projects import test_stock as fixture
from projects.models import LegalEntity,COMPANY_TAX_ID,RepairConsumption
from repairs.models import Report
from .test_progress import photo


class ProgressBrowserTests(StaticLiveServerTestCase):
    make_job=fixture.StockTests.make_job
    def test_mobile_daily_photos_keep_multiple_stages_and_timeline(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        password=secrets.token_urlsafe(20);self.worker.set_password(password);self.worker.save()
        job=self.make_job()
        env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,
            'ARMADA_PROGRESS_PATH':reverse('repair_report',args=[job.pk,'progress']),
            'ARMADA_PHOTO_BASE64':base64.b64encode(photo().read()).decode()}
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/progress-browser.cjs')],env=env,capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        report=Report.objects.get(job=job);self.assertEqual(report.context['progress']['stages'],['excavation','ddk'])
        self.assertEqual(report.attachments.count(),1);self.assertFalse(RepairConsumption.objects.exists())
