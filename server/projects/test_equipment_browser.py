import os,secrets,subprocess,tempfile
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.utils import timezone
from django.test import override_settings
from accounts.models import User
from documents.models import Document
from .models import LegalEntity,ProjectObject,Equipment,EquipmentAssignment,EquipmentDocument,COMPANY_TAX_ID

class EquipmentBrowserTests(StaticLiveServerTestCase):
    def test_equipment_assignment_documents_and_excel_in_real_ui(self):
        password=secrets.token_urlsafe(24)
        leader=User.objects.create_user('+70000000001',password,leader_slot=1,full_name='ТЕСТ Руководитель',must_change_password=False)
        company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})[0]
        obj=ProjectObject.objects.create(legal_entity=company,name='ТЕСТ Омск–Татарск')
        doc=Document.objects.create(legal_entity=company,title='ТЕСТ Страховка экскаватора',section='general',created_by=leader)
        env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password,ARMADA_OBJECT_ID=str(obj.pk),ARMADA_DOCUMENT_ID=str(doc.pk),ARMADA_TODAY=timezone.localdate().isoformat())
        with tempfile.TemporaryDirectory() as root,override_settings(PRIVATE_DOCUMENT_ROOT=root):
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/equipment-browser.cjs')],env=env,capture_output=True,text=True,timeout=180)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(Equipment.objects.count(),1);self.assertEqual(EquipmentAssignment.objects.count(),1);self.assertEqual(EquipmentDocument.objects.count(),2);self.assertEqual(EquipmentDocument.objects.filter(remind=True).count(),1)
