import os,secrets,subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from . import test_stock as base,test_release6241 as features,equipment
from .models import LegalEntity,COMPANY_TAX_ID,EquipmentRental,StockItem
from repairs.models import Report

class Release6241BrowserTests(StaticLiveServerTestCase):
    wh=base.StockTests.wh;data=base.StockTests.data;item=base.StockTests.item;move=base.StockTests.move;crew=base.StockTests.crew;make_job=base.StockTests.make_job;balance=base.StockTests.balance
    machine=features.Release6241Tests.machine;reserved=features.Release6241Tests.reserved
    def test_existing_equipment_card_and_inspection_change_on_phone(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'});base.StockTests.setUpTestData.__func__(type(self))
        self.section.diameter_mm=720;self.section.save();password=secrets.token_urlsafe(24)
        for u in [self.leader,self.worker]:u.set_password(password);u.save()
        eq=self.machine();placement=equipment.assign(self.leader,eq.pk,eq.version,self.obj,self.contract,self.today,crew_assignment=self.crew())
        rental=EquipmentRental.objects.get(placement=placement);job,wh,item=self.reserved()
        env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,'ARMADA_EQUIPMENT_PATH':reverse('equipment_detail',args=[eq.pk]),'ARMADA_JOB_PATH':reverse('repair_detail',args=[job.pk]),'ARMADA_NEEDS_PATH':reverse('repair_materials')}
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/release6241-browser.cjs')],env=env,capture_output=True,text=True,timeout=180)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr);self.assertEqual(rental.days.get().hours,10)
        item.refresh_from_db();self.assertIsNone(item.reserved_job_id);self.assertEqual(self.balance(wh,item),1);job.refresh_from_db();self.assertEqual(job.length_m,1.5);self.assertTrue(Report.objects.filter(job=job,kind='plan_change').exists())
