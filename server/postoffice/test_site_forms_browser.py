import json
import os
import subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from accounts.models import User
from .models import SiteRequest


class SiteFormsBrowser(StaticLiveServerTestCase):
    def test_real_forms_receipts_retry_and_private_inbox(self):
        User.objects.create_user('+70000000401','123456',full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/public-forms-browser.cjs')],
            env=os.environ|{'ARMADA_TEST_URL':self.live_server_url},capture_output=True,text=True,timeout=150)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(SiteRequest.objects.count(),4)
        candidate=SiteRequest.objects.get(kind='candidate')
        self.assertEqual(candidate.data['travel'],'Готов(а)')
        self.assertEqual(bytes(candidate.resume_data),b'%PDF-1.4\nsynthetic browser resume')
        self.assertEqual(SiteRequest.objects.filter(delivery_state='queued').count(),4)
