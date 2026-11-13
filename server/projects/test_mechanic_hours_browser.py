import os,secrets,subprocess
from django.conf import settings
from django.test import override_settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from . import test_equipment as fixture,test_mechanic_hours as hours
from .models import LegalEntity,COMPANY_TAX_ID


@override_settings(ARMADA_MAX_ENABLED=True)
class MechanicHoursBrowserTests(StaticLiveServerTestCase):
    grant=fixture.EquipmentTests.grant;make=fixture.EquipmentTests.make;data=fixture.EquipmentTests.data
    def test_new_login_prompt_then_existing_hours_on_phone(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.EquipmentTests.setUpTestData.__func__(type(self))
        hours.MechanicHoursTests.setUp(self)
        password=secrets.token_urlsafe(20);self.mechanic.set_password(password);self.mechanic.save()
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/mechanic-hours-browser.cjs')],env=os.environ|{
            'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,
            'ARMADA_CARD_PATH':reverse('equipment_detail',args=[self.item.pk])},capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
