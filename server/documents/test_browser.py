import os
import secrets
import subprocess
import tempfile
from django.conf import settings
from django.test import override_settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from accounts.models import User, AccessGrant
from projects.models import LegalEntity, ProjectObject, ObjectAccess, COMPANY_TAX_ID

class DocumentsBrowserAcceptance(StaticLiveServerTestCase):
    def test_document_workflow_in_browser(self):
        company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ООО Армада-Сибирь'})[0]
        password=secrets.token_urlsafe(24)
        leader=User.objects.create_user('+70000000001',password,full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        employee=User.objects.create_user('+70000000003',password,full_name='ТЕСТ Мастер',role='foreman',must_change_password=False)
        obj=ProjectObject.objects.create(legal_entity=company,name='Учебный объект')
        ObjectAccess.objects.create(project_object=obj,user=employee,granted_by=leader)
        for permission in ['read','upload','replace','download']:
            AccessGrant.objects.create(user=employee,permission='document.'+permission,granted_by=leader)
        from finance.models import FinancialScope,FinancialImport,OpeningSnapshot,PendingWork
        source=FinancialImport.objects.create(sha256='a'*64,original_name='synthetic-finance.xlsx',uploaded_by=leader)
        scope=FinancialScope.objects.create(legal_entity=company,title='Учебная финансовая карточка',created_by=leader)
        OpeningSnapshot.objects.create(scope=scope,source=source,total_defects=10,repaired_defects=4,advance_received=5000000,accepted=8000000,paid_for_work=6000000,advance_offset=2000000,declared_advance_balance=3000000,declared_pending=1000000)
        PendingWork.objects.create(scope=scope,title='Учебное выполнение',amount=1000000,source=source)
        env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password)
        with tempfile.TemporaryDirectory(prefix='armada-browser-docs-') as root,override_settings(PRIVATE_DOCUMENT_ROOT=root):
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/documents-browser.cjs')],env=env,capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
