import os
import secrets
import subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from accounts.models import User
from .models import LegalEntity, COMPANY_TAX_ID

class ProjectBrowserAcceptance(StaticLiveServerTestCase):
    def test_objects_contracts_in_browser(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ООО «Армада-Сибирь»'})
        password=secrets.token_urlsafe(24)
        User.objects.create_user('+70000000001',password,full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        User.objects.create_user('+70000000003',password,full_name='ТЕСТ Мастер',role='foreman',must_change_password=False)
        env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password)
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/projects-browser.cjs')],env=env,capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
