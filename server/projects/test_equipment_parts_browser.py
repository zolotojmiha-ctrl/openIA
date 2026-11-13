import os,secrets,subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from . import test_equipment as fixture
from .models import LegalEntity,COMPANY_TAX_ID,Equipment,EquipmentMaintenancePart,StockMovement


class EquipmentPartsBrowserTests(StaticLiveServerTestCase):
    grant=fixture.EquipmentTests.grant
    def test_mechanic_creates_vehicle_and_maintenance_article_on_phone(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.EquipmentTests.setUpTestData.__func__(type(self));self.grant()
        password=secrets.token_urlsafe(20);self.mechanic.set_password(password);self.mechanic.save()
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/equipment-parts-browser.cjs')],
            env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password},capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        item=Equipment.objects.get(inventory_number='TEST-SOBOL-001');part=EquipmentMaintenancePart.objects.get(equipment=item)
        self.assertEqual(part.article,'00123/45-AB');self.assertEqual(part.created_by,self.mechanic)
        self.assertFalse(StockMovement.objects.exists())
