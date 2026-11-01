import os,secrets,subprocess,tempfile
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.urls import reverse
from . import test_stock as base,test_release625 as feature,equipment
from .models import LegalEntity,COMPANY_TAX_ID,EquipmentRental,CounterpartyAgreement
from accounts.models import AccessGrant

class Release625BrowserTests(StaticLiveServerTestCase):
    wh=base.StockTests.wh;data=base.StockTests.data;item=base.StockTests.item;move=base.StockTests.move;crew=base.StockTests.crew;make_job=base.StockTests.make_job;balance=base.StockTests.balance
    machine=feature.Release625Tests.machine;other_scope=feature.Release625Tests.other_scope
    def test_phone_contract_upload_and_compact_register(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'});base.StockTests.setUpTestData.__func__(type(self));password=secrets.token_urlsafe(24)
        self.section.diameter_mm=720;self.section.pipeline='ТЕСТ Омск–Иркутск';self.section.name='ТЕСТ Длинное название участка без повторов в строках';self.section.save()
        for u in [self.leader,self.worker]:u.set_password(password);u.save()
        for permission in ('finance.read','finance.write','rental.manage'):AccessGrant.objects.create(user=self.worker,permission=permission,granted_by=self.leader)
        obj,contract=self.other_scope()
        for _ in range(3):
            j=self.make_job();j.method='p1';j.length_m=4;j.save()
        eq=self.machine();placement=equipment.assign(self.leader,eq.pk,eq.version,self.obj,self.contract,self.today,crew_assignment=self.crew())
        env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,'ARMADA_OBJECT_IDS':str(self.obj.pk)+','+str(obj.pk),'ARMADA_CONTRACT_IDS':str(self.contract.pk)+','+str(contract.pk),'ARMADA_EQUIPMENT_PATH':reverse('equipment_detail',args=[eq.pk])}
        with tempfile.TemporaryDirectory() as tmp,override_settings(PRIVATE_DOCUMENT_ROOT=tmp):
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/release625-browser.cjs')],env=env,capture_output=True,text=True,timeout=200)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        agreement=CounterpartyAgreement.objects.get(number='ТЕСТ 625 БРАУЗЕР');self.assertEqual(agreement.amount,150000);self.assertEqual(agreement.documents.count(),2);self.assertEqual(agreement.customer_contracts.count(),2);self.assertEqual(agreement.project_objects.count(),2)
        self.assertEqual(EquipmentRental.objects.get(placement=placement).days.get().hours,10)
