import os,subprocess
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.conf import settings
from accounts.models import User
from projects.models import LegalEntity,COMPANY_TAX_ID,Contract,ProjectObject,ContractObject,CounterpartyAgreement
class UpdateBrowserTests(StaticLiveServerTestCase):
    def test_agreement_entry_and_responsive_navigation(self):
        company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})[0]
        User.objects.create_user('+79999980201','123456',full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        contract=Contract.objects.create(legal_entity=company,number='ТЕСТ Контракт заказчика',subject='Работы',work_kinds=['repairs'])
        obj=ProjectObject.objects.create(legal_entity=company,name='ТЕСТ Объект');ContractObject.objects.create(contract=contract,project_object=obj)
        env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_CUSTOMER_ID':str(contract.pk),'ARMADA_OBJECT_ID':str(obj.pk)}
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/update614-browser.cjs')],env=env,capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(Contract.objects.count(),1);self.assertEqual(CounterpartyAgreement.objects.get().kind,'housing')
