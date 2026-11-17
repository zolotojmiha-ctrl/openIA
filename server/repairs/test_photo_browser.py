import os,io,base64,secrets,subprocess,tempfile
from pathlib import Path
from PIL import Image
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.urls import reverse
from projects import test_stock as fixture
from projects.models import LegalEntity,COMPANY_TAX_ID
from . import test_progress as progress
from .services import report_work


class PhotoManagementBrowserTests(StaticLiveServerTestCase):
    make_job=fixture.StockTests.make_job
    data=progress.ProgressTests.data
    def test_mobile_photo_management_permissions_and_destructive_confirmation(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'TEST'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        password=secrets.token_urlsafe(20)
        for user in (self.worker,self.leader):user.set_password(password);user.save()
        job=self.make_job();root=tempfile.TemporaryDirectory();self.addCleanup(root.cleanup)
        with override_settings(PRIVATE_DOCUMENT_ROOT=root.name):
            report_work(self.worker,job.pk,'note',self.data(job))
            image=io.BytesIO();Image.new('RGB',(20,20),'red').save(image,'PNG')
            env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,'ARMADA_PHOTO_JOB':reverse('repair_detail',args=[job.pk]),'ARMADA_PHOTO_BASE64':base64.b64encode(image.getvalue()).decode()}
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/photo-management-browser.cjs')],env=env,capture_output=True,text=True,timeout=90)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
