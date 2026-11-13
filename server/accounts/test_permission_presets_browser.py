import os,json,subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from projects.models import LegalEntity,COMPANY_TAX_ID
from . import tests as fixture
from .models import User,ROLES
from .presets import PRESETS

class PermissionPresetsBrowser(StaticLiveServerTestCase):
    def test_all_roles_preview_and_saved_employee_grants(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.AccountTests.setUpTestData.__func__(type(self))
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/permission-presets-browser.cjs')],env=os.environ|{
            'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':self.secret,
            'ARMADA_TEST_PROFILES':json.dumps({r:PRESETS[r]['permissions'] for r,label in ROLES}),
            'ARMADA_TEST_OLD_USER':str(self.employee.pk)},capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        created=User.objects.get(phone='+70000000999')
        self.assertEqual(set(created.grants.values_list('permission',flat=True)),set(PRESETS['foreman']['permissions']))
        self.assertFalse(self.employee.grants.exists())
