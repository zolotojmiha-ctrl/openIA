import os,subprocess
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.conf import settings
from accounts.models import User
from .models import LegalEntity,COMPANY_TAX_ID,Contract,ProjectObject
class ContractFirstBrowser(StaticLiveServerTestCase):
    def test_contract_then_object_from_empty_installation(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        User.objects.create_user('+79999980401','123456',leader_slot=1,full_name='ТЕСТ Руководитель',must_change_password=False)
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/contract-first-browser.cjs')],env=os.environ|{'ARMADA_TEST_URL':self.live_server_url},capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        from repairs.models import Job
        self.assertEqual(Job.objects.count(),1)
        self.assertEqual(Contract.objects.count(),1);self.assertEqual(ProjectObject.objects.count(),1);self.assertEqual(Contract.objects.get().objects_linked.count(),1)
