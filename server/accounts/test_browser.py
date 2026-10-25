import os
from .passwords import temporary_password
import subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from .models import User

class BrowserAcceptance(StaticLiveServerTestCase):
    def test_working_accounts_in_browser(self):
        secret=temporary_password()
        for slot in (1,2):
            User.objects.create_user(f'+7000000000{slot}',secret,full_name=f'ТЕСТ Руководитель {slot}',leader_slot=slot)
        env=os.environ.copy()
        env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=secret,ARMADA_TEST_NEW_PASSWORD='123456')
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/accounts-browser.cjs')],env=env,capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
