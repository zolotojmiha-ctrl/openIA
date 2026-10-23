import os,secrets,subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from accounts.models import User,AccessGrant
from .models import LegalEntity,ProjectObject,Contract,ContractObject,ObjectAccess,ContractAccess,COMPANY_TAX_ID

class ClosureBrowserTests(StaticLiveServerTestCase):
    def test_request_return_archive_and_restore_on_phone(self):
        password=secrets.token_urlsafe(24)
        leader=User.objects.create_user('+79990009201',password,leader_slot=1,full_name='ТЕСТ Руководитель',must_change_password=False)
        tech=User.objects.create_user('+79990009202',password,role='technician',full_name='ТЕСТ Техник',must_change_password=False)
        company,_=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        obj=ProjectObject.objects.create(legal_entity=company,name='ТЕСТ Омск — Татарск')
        contract=Contract.objects.create(legal_entity=company,number='ТЕСТ-1726',subject='ТЕСТ Устранение дефектов',status='active',work_kinds=['repairs'])
        ContractObject.objects.create(contract=contract,project_object=obj)
        ObjectAccess.objects.create(user=tech,project_object=obj,granted_by=leader)
        ContractAccess.objects.create(user=tech,contract=contract,show_number=True,granted_by=leader)
        AccessGrant.objects.create(user=tech,permission='projects.manage',granted_by=leader)
        env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password,ARMADA_CLOSURE_CONTRACT=str(contract.pk))
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/closure-browser.cjs')],env=env,capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        contract.refresh_from_db();self.assertEqual(contract.status,'active');self.assertEqual(contract.closure_events.count(),5)
