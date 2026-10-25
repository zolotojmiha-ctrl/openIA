import os,secrets,subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from accounts.models import User,AccessGrant
from .models import LegalEntity,ProjectObject,Contract,ContractObject,ObjectAccess,ContractAccess,COMPANY_TAX_ID,ContractChange

class CustomerChangesBrowserTests(StaticLiveServerTestCase):
    def test_submit_review_revision_and_commercial_status(self):
        company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})[0]
        password=secrets.token_urlsafe(24)
        leader=User.objects.create_user('+70000000621',password,full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        tech=User.objects.create_user('+70000000622',password,full_name='ТЕСТ Техник',role='technician',must_change_password=False)
        obj=ProjectObject.objects.create(legal_entity=company,name='ТЕСТ 214 км')
        contract=Contract.objects.create(legal_entity=company,number='ТЕСТ-214',subject='ТЕСТ подготовка к диагностике',work_kinds=['diagnostics'])
        ContractObject.objects.create(contract=contract,project_object=obj)
        ObjectAccess.objects.create(user=tech,project_object=obj,granted_by=leader)
        ContractAccess.objects.create(user=tech,contract=contract,granted_by=leader,show_number=True)
        AccessGrant.objects.create(user=tech,permission='contract.changes',granted_by=leader)
        env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password,ARMADA_CHANGE_CONTRACT=str(contract.pk))
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/customer-changes-browser.cjs')],env=env,capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        change=ContractChange.objects.get();self.assertEqual(change.state,'approved');self.assertEqual(change.revisions.count(),2);self.assertEqual(change.active_revision.scope,'Секции 7–12')
        self.assertEqual(change.active_revision.commercial_events.get().status,'not_required')
