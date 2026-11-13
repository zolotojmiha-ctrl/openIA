import os,secrets,tempfile,subprocess,zipfile
from pathlib import Path
from django.conf import settings
from django.test import override_settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from accounts.models import User
from projects import test_stock as fixture,test_release624 as sample,rentals
from projects.models import LegalEntity,COMPANY_TAX_ID,RentalAccess,RentalDispute
from repairs.models import Attachment

class RemainingBrowserTests(StaticLiveServerTestCase):
    crew=fixture.StockTests.crew;rental=sample.Release624Tests.rental;make_job=fixture.StockTests.make_job
    def test_ddk_and_owner_workflow_with_no_internal_financial_access(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        hire=self.rental();rentals.save_day(self.worker,hire.pk,self.today,'work')
        job=self.make_job();job.method='grind';job.length_m=None;job.wall_mm=None;job.save()
        password=secrets.token_urlsafe(20)
        owner=User.objects.create_user('+70000000004',password,role='lessor',full_name='ТЕСТ арендодатель',must_change_password=False)
        for user in (self.leader,self.worker):user.set_password(password);user.save()
        with tempfile.TemporaryDirectory() as tmp,override_settings(PRIVATE_DOCUMENT_ROOT=str(Path(tmp)/'files')):
            output=Path(tmp)/'owner.xlsx'
            env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,'ARMADA_MASTER_PHONE':self.worker.phone,'ARMADA_LEADER_PHONE':self.leader.phone,'ARMADA_OWNER_PHONE':owner.phone,'ARMADA_OWNER_ID':str(owner.pk),'ARMADA_RENTAL':str(hire.pk),'ARMADA_REPORT_PATH':reverse('repair_report',args=[job.pk,'repaired']),'ARMADA_EQUIPMENT_PATH':reverse('equipment_detail',args=[hire.placement.equipment_id]),'ARMADA_OWNER_XLSX':str(output)}
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/remaining-updates-browser.cjs')],env=env,capture_output=True,text=True,timeout=120)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            job.refresh_from_db();self.assertEqual(job.method,'p1');self.assertEqual(job.state,'planned');self.assertEqual(Attachment.objects.count(),1)
            self.assertFalse(RentalAccess.objects.get().financial);self.assertIsNotNone(RentalDispute.objects.get().resolved_at)
            self.assertEqual(hire.days.get().hours,10)
            with zipfile.ZipFile(output) as book:self.assertNotIn('Начислено',book.read('xl/worksheets/sheet1.xml').decode())
