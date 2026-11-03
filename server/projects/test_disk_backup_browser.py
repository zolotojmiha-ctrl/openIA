import os,secrets,subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from . import test_stock as fixture
from .models import LegalEntity,COMPANY_TAX_ID,DiskBackupSettings

class BackupBrowser(StaticLiveServerTestCase):
    def test_setup_on_phone(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        password=secrets.token_urlsafe(20);self.leader.set_password(password);self.leader.save()
        env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password}
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/disk-backup-browser.cjs')],env=env,capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        config=DiskBackupSettings.objects.get(pk=1)
        self.assertTrue(config.key_confirmed);self.assertFalse(config.enabled)
