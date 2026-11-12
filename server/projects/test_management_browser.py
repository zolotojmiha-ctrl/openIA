import os,secrets,subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from . import test_stock as fixture
from .models import LegalEntity,COMPANY_TAX_ID,Equipment,EquipmentAssignment,EquipmentIssue


class ManagementBrowserTests(StaticLiveServerTestCase):
    def test_master_submits_and_leader_resolves_equipment_issue(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        password=secrets.token_urlsafe(20)
        for user in (self.worker,self.leader):user.set_password(password);user.save()
        item=Equipment.objects.create(legal_entity=self.company,name='ТЕСТ Экскаватор',category='ТЕСТ',inventory_number='TEST-ISSUE',created_by=self.leader)
        EquipmentAssignment.objects.create(equipment=item,project_object=self.obj,contract=self.contract,start_date=self.today,created_by=self.leader)
        env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,'ARMADA_EQUIPMENT_PATH':reverse('equipment_detail',args=[item.pk])}
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/management-browser.cjs')],env=env,capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        row=EquipmentIssue.objects.get();self.assertEqual(row.state,'closed');self.assertEqual(row.author,self.worker)
