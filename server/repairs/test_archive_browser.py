import json,os,secrets,subprocess,tempfile
from pathlib import Path
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from projects import test_stock as fixture
from projects.models import LegalEntity,COMPANY_TAX_ID,Contract,ContractObject
from .models import Job,Defect
from .test_archive_import import plan_for
from . import archive_import as service

class ArchiveBrowserTests(StaticLiveServerTestCase):
    def test_native_lk_import_and_mobile_archive(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self));password=secrets.token_urlsafe(24)
        self.leader.set_password(password);self.leader.save()
        contract=Contract.objects.create(legal_entity=self.company,number=service.TITLE,subject='ТЕСТ',work_kinds=['repairs'],status='planned')
        ContractObject.objects.create(contract=contract,project_object=self.obj)
        with tempfile.TemporaryDirectory() as root,override_settings(PRIVATE_DOCUMENT_ROOT=root):
            path=Path(root)/'test-history.json';path.write_text(json.dumps(plan_for(contract,self.obj),ensure_ascii=False))
            env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password,ARMADA_ARCHIVE_ID=str(contract.pk),ARMADA_ARCHIVE_FILE=str(path))
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/archive-import-browser.cjs')],env=env,capture_output=True,text=True,timeout=90)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(Job.objects.filter(contract=contract).count(),2);self.assertEqual(Defect.objects.filter(contract=contract).count(),3)
