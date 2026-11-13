import os,secrets,subprocess
from decimal import Decimal
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from projects.models import LegalEntity,COMPANY_TAX_ID,ContractObject,ObjectAccess
from accounts.models import AccessGrant
from . import tests as fixture

class RepairOverviewBrowser(StaticLiveServerTestCase):
    def test_leader_grants_scope_and_reading_without_changing_assignee(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.RepairsTests.setUpTestData.__func__(type(self))
        self.hidden.contract=self.contract;self.hidden.save()
        defect=self.hidden.defects.get();defect.contract=self.contract;defect.kilometre=Decimal('135.651');defect.save()
        ContractObject.objects.create(contract=self.contract,project_object=self.obj2)
        password=secrets.token_urlsafe(24)
        for user in (self.leader,self.worker):user.set_password(password);user.save()
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/repair-overview-browser.cjs')],env=os.environ|{
            'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,
            'ARMADA_EMPLOYEE_PATH':reverse('employee_edit',args=[self.worker.pk]),
            'ARMADA_ASSIGNMENTS_PATH':reverse('object_assignments',args=[self.obj2.pk]),
            'ARMADA_OTHER_JOB_PATH':reverse('repair_detail',args=[self.hidden.pk]),
            'ARMADA_OTHER_START_PATH':reverse('repair_report',args=[self.hidden.pk,'start']),
            'ARMADA_WORKER_ID':str(self.worker.pk)},capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.hidden.refresh_from_db();self.assertEqual(self.hidden.assigned_to_id,self.other.pk)
        self.assertTrue(AccessGrant.objects.filter(user=self.worker,permission='repairs.overview').exists())
        self.assertTrue(ObjectAccess.objects.filter(user=self.worker,project_object=self.obj2).exists())
