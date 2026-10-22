import os,secrets,subprocess,tempfile
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from accounts.models import User
from .models import LegalEntity,ProjectObject,Contract,ContractObject,ObjectAccess,ContractAccess,Subcontractor,SubcontractOrder,SubcontractResult,SubcontractDecision,COMPANY_TAX_ID

class SubcontractBrowserTests(StaticLiveServerTestCase):
    def test_company_order_access_and_result_with_private_photo(self):
        password=secrets.token_urlsafe(24)
        leader=User.objects.create_user('+70000000001',password,leader_slot=1,full_name='ТЕСТ Руководитель',must_change_password=False)
        rep=User.objects.create_user('+70000000003',password,role='subcontractor',full_name='ТЕСТ Представитель монтажа',must_change_password=False)
        entity=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})[0];obj=ProjectObject.objects.create(legal_entity=entity,name='ТЕСТ НПС Татарская')
        contract=Contract.objects.create(legal_entity=entity,number='СЕКРЕТНЫЙ ДОГОВОР',subject='Закрытый предмет',work_kinds=['valves','repairs']);ContractObject.objects.create(contract=contract,project_object=obj)
        ObjectAccess.objects.create(user=rep,project_object=obj,granted_by=leader);ContractAccess.objects.create(user=rep,contract=contract,granted_by=leader)
        env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password,ARMADA_SUB_REP=str(rep.pk),ARMADA_SUB_OBJECT=str(obj.pk),ARMADA_SUB_CONTRACT=str(contract.pk))
        with tempfile.TemporaryDirectory() as root,override_settings(PRIVATE_DOCUMENT_ROOT=root):
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/subcontract-browser.cjs')],env=env,capture_output=True,text=True,timeout=180)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr);self.assertEqual(Subcontractor.objects.count(),1);self.assertEqual(SubcontractOrder.objects.get().state,'confirmed');self.assertEqual(SubcontractResult.objects.count(),2);self.assertEqual(SubcontractDecision.objects.count(),2)
