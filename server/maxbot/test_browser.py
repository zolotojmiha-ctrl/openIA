import os
import secrets
import subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from projects import test_stock as fixture
from projects.models import LegalEntity, COMPANY_TAX_ID
from .models import Binding
from .tests import CONFIG


@override_settings(**CONFIG)
class MaxBrowser(StaticLiveServerTestCase):
    def test_profile_connection_on_phone_and_desktop(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID, defaults={'name': 'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        password = secrets.token_urlsafe(20)
        self.worker.set_password(password); self.worker.save()
        Binding.objects.create(user=self.worker, max_user=101, chat=201, session_version=self.worker.session_version)
        env = os.environ | {'ARMADA_TEST_URL': self.live_server_url, 'ARMADA_TEST_PASSWORD': password}
        result = subprocess.run(['node', str(settings.BASE_DIR/'tests/max-browser.cjs')],
                                 env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(Binding.objects.filter(active=True).exists())
