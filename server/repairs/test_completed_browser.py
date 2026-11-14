import json,os,secrets,subprocess,tempfile
from pathlib import Path
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from projects import test_stock as fixture
from projects.models import LegalEntity,COMPANY_TAX_ID
from .test_completed_import import plan_for
from .models import Job,Defect


class CompletedBrowserTests(StaticLiveServerTestCase):
    def test_current_contract_import_and_mobile_preview(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self));password=secrets.token_urlsafe(24)
        self.leader.set_password(password);self.leader.save()
        self.contract.number='ТЕСТ ТЗС-1731-2025';self.contract.status='active';self.contract.save()
        fixture.StockTests.make_job(self)
        with tempfile.TemporaryDirectory() as root,override_settings(PRIVATE_DOCUMENT_ROOT=root):
            path=Path(root)/'test-completed.json';path.write_text(json.dumps(plan_for(self.contract,self.obj),ensure_ascii=False))
            env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,'ARMADA_COMPLETED_ID':str(self.contract.pk),'ARMADA_COMPLETED_FILE':str(path)}
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/completed-import-browser.cjs')],env=env,capture_output=True,text=True,timeout=90)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.contract.refresh_from_db();self.assertEqual(self.contract.status,'active')
        self.assertEqual(Job.objects.filter(state='confirmed').count(),2);self.assertEqual(Defect.objects.count(),3)
