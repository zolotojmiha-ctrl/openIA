import os
import secrets
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from accounts.models import AccessGrant, User
from accounts.presets import PRESETS
from repairs.importer import read_book
from django.core.files.uploadedfile import SimpleUploadedFile
from . import test_stock as base, test_release624 as previous
from .models import LegalEntity, COMPANY_TAX_ID, ObjectAccess, ContractAccess, StockMovement


class StockReportsBrowser(StaticLiveServerTestCase):
    wh = base.StockTests.wh
    data = base.StockTests.data
    item = base.StockTests.item
    move = base.StockTests.move
    make_job = base.StockTests.make_job
    profile = previous.Release624Tests.profile

    def test_pto_phone_access_norms_and_both_excel_downloads(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID, defaults={'name': 'ТЕСТ'})
        base.StockTests.setUpTestData.__func__(type(self))
        self.section.diameter_mm = 720
        self.section.save()
        password = secrets.token_urlsafe(24)
        pto = User.objects.create_user('+70000000731', password, role='pto', full_name='ТЕСТ ПТО', must_change_password=False)
        for permission in PRESETS['pto']['permissions']:
            AccessGrant.objects.create(user=pto, permission=permission, granted_by=self.leader)
        ObjectAccess.objects.create(user=pto, project_object=self.obj, granted_by=self.leader)
        ContractAccess.objects.create(user=pto, contract=self.contract, granted_by=self.leader)
        profile, warehouse, items = self.profile()
        self.make_job()
        piece = self.item(kind='coupling', unit='pcs', method='p2', length_mm=1000, wall_mm=8, diameter_mm=720)
        self.move(piece, dst=warehouse, q=1)
        movements = StockMovement.objects.count()
        with tempfile.TemporaryDirectory() as folder, override_settings(PRIVATE_DOCUMENT_ROOT=folder):
            env = os.environ | {'ARMADA_TEST_URL': self.live_server_url, 'ARMADA_TEST_PASSWORD': password,
                                'ARMADA_TEST_DOWNLOADS': folder}
            result = subprocess.run(['node', str(settings.BASE_DIR / 'tests/stock-reports-browser.cjs')], env=env, capture_output=True, text=True, timeout=150)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            needs = read_book(SimpleUploadedFile('needs.xlsx', (Path(folder) / 'repair-materials.xlsx').read_bytes()), sheet_name='Потребность')['cells']
            coverage = read_book(SimpleUploadedFile('couplings.xlsx', (Path(folder) / 'coupling-coverage.xlsx').read_bytes()), sheet_name='Муфты')['cells']
            self.assertNotIn((2,1),needs)  # Material and coupling demand is fully covered.
            self.assertNotIn((2,1),coverage)  # Fully covered work has no shortage row.
        self.assertEqual(StockMovement.objects.count(), movements)
