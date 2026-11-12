import os
import secrets
import subprocess
from datetime import timedelta
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.utils import timezone
from projects import test_stock as fixture
from projects.models import LegalEntity, COMPANY_TAX_ID
from .models import Binding
from .tests import CONFIG


@override_settings(**CONFIG)
class MaxBrowser(StaticLiveServerTestCase):
    def test_first_password_change_offers_optional_max_connection(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID, defaults={'name': 'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        password = secrets.token_urlsafe(20)
        self.worker.set_password(password)
        self.worker.must_change_password = True
        self.worker.temporary_expires_at = timezone.now() + timedelta(hours=1)
        self.worker.save()
        env = os.environ | {'ARMADA_TEST_URL': self.live_server_url, 'ARMADA_TEST_PASSWORD': password,
                            'ARMADA_TEST_PHONE': self.worker.phone, 'ARMADA_TEST_NEW_PASSWORD': secrets.token_urlsafe(20)}
        result = subprocess.run(['node', str(settings.BASE_DIR/'tests/max-onboarding-browser.cjs')],
                                 env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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

    def test_existing_unbound_user_can_defer_each_login_or_connect(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID, defaults={'name': 'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        password=secrets.token_urlsafe(20)
        self.worker.set_password(password);self.worker.save()
        env=os.environ | {'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,'ARMADA_TEST_PHONE':self.worker.phone}
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/max-login-prompt-browser.cjs')],env=env,capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
