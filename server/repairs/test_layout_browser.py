import os,secrets,subprocess,tempfile
from pathlib import Path
from django.conf import settings
from django.test import override_settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from projects import test_stock as fixture
from projects.models import LegalEntity,COMPANY_TAX_ID
from .models import Job,Defect,JobDefect
from .test_layouts import xlsx

class LayoutBrowserTests(StaticLiveServerTestCase):
    def test_layout_upload_and_foreman_distance_at_phone_widths(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self));password=secrets.token_urlsafe(24)
        for user in (self.leader,self.worker):user.set_password(password);user.save()
        for km,section,number in [(100,'100','123'),(106,'120','987')]:
            j=Job.objects.create(contract=self.contract,section=self.section,created_by=self.leader,assigned_to=self.worker,method='grind')
            d=Defect.objects.create(contract=self.contract,section=self.section,kilometre=km,pipe_section=section,number=number);JobDefect.objects.create(job=j,defect=d)
        with tempfile.TemporaryDirectory() as root,override_settings(PRIVATE_DOCUMENT_ROOT=root):
            path=Path(root)/'test-layout.xlsx';path.write_bytes(xlsx().read())
            env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password,ARMADA_LAYOUT_SECTION=str(self.section.pk),ARMADA_LAYOUT_JOB=str(j.pk),ARMADA_LAYOUT_FILE=str(path))
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/layouts-browser.cjs')],env=env,capture_output=True,text=True,timeout=90)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
