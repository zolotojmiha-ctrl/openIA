import json,os,secrets,subprocess
from django.conf import settings
from django.urls import reverse
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from . import test_stock as fixture
from .models import LegalEntity,COMPANY_TAX_ID,DemandMailing
from postoffice.models import Mailbox,Letter

class DemandMailBrowser(StaticLiveServerTestCase):
    make_job=fixture.StockTests.make_job
    def test_preview_settings_dn_and_queue_on_phone(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        password=secrets.token_urlsafe(20);self.leader.set_password(password);self.leader.save()
        self.section.diameter_mm=720;self.section.save()
        job=self.make_job()
        box=Mailbox.objects.create(address='noreply@example.org',state='active',created_by=self.leader)
        config=DemandMailing.objects.create(name='ТЕСТ заказчик',owner=self.leader,mailbox=box,recipients='customer@example.org')
        config.project_objects.add(self.obj)
        env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,'ARMADA_DEMAND_CONFIG':str(config.pk),'ARMADA_MATERIALS_PATH':reverse('repair_materials')}
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/demand-mail-browser.cjs')],env=env,capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(Letter.objects.count(),1)
        self.assertEqual(Letter.objects.get().state,'queued')
        config.refresh_from_db();self.assertFalse(config.enabled)
