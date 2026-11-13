import os,secrets,subprocess,tempfile,zipfile
from pathlib import Path
from datetime import timedelta
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from projects import test_stock as fixture
from projects.models import LegalEntity,COMPANY_TAX_ID,ContractAccess,ObjectAccess
from accounts.models import User
from .models import Defect,JobDefect,Report

class CompletionBrowserTests(StaticLiveServerTestCase):
    make_job=fixture.StockTests.make_job
    def test_actual_register_on_phone_and_desktop(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        password=secrets.token_urlsafe(20)
        pto=User.objects.create_user('+70000000004',password,role='pto',full_name='ТЕСТ ПТО',must_change_password=False)
        ObjectAccess.objects.create(user=pto,project_object=self.obj,granted_by=self.leader)
        ContractAccess.objects.create(user=pto,contract=self.contract,granted_by=self.leader)
        job=self.make_job();job.state='confirmed';job.save()
        defect=Defect.objects.create(contract=self.contract,section=self.section,kilometre=2,number='ТЕСТ второй')
        JobDefect.objects.create(job=job,defect=defect)
        Report.objects.create(job=job,author=self.worker,kind='complete',method='p1',length_m=3,composition='1,5 + 1,5',performed_date=self.today-timedelta(days=2),context={'actual_coating_length_m':'3.8'})
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'register.xlsx'
            env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PHONE':pto.phone,'ARMADA_TEST_PASSWORD':password,
                'ARMADA_TEST_DATE':str(self.today),'ARMADA_TEST_MASTER':str(self.worker.pk),'ARMADA_TEST_XLSX':str(path),'ARMADA_TEST_SCREENSHOT':'/tmp/pto-register-browser.png'}
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/pto-register-browser.cjs')],env=env,capture_output=True,text=True,timeout=90)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            with zipfile.ZipFile(path) as book:self.assertIn('Фактическая длина покрытия',book.read('xl/worksheets/sheet1.xml').decode())
