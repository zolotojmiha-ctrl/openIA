import json
import os
import subprocess
from django.conf import settings
from decimal import Decimal
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from projects import stock, test_stock as fixtures
from projects.models import LegalEntity, COMPANY_TAX_ID, ProjectObject, WarehouseAccess, StockBalance, StockMovement


class WarehouseActionsBrowser(StaticLiveServerTestCase):
    data = fixtures.StockTests.data

    def test_visible_actions_multi_area_and_readonly_access(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID, defaults={'name': 'ТЕСТ'})
        fixtures.StockTests.setUpTestData.__func__(type(self))
        for user in [self.leader, self.worker]:
            user.set_password('123456')
            user.save()
        other = ProjectObject.objects.create(legal_entity=self.company, name='ТЕСТ Второй участок')
        warehouse = stock.save_warehouse(self.leader, None, None, {
            'name': 'ТЕСТ Центральный склад', 'location': 'ТЕСТ Адрес склада',
            'served_objects': [self.obj], 'served_brigades': [],
        })
        item = fixtures.StockTests.item(self)
        fixtures.StockTests.move(self, item, q=10, dst=warehouse)
        WarehouseAccess.objects.create(warehouse=warehouse, user=self.worker, granted_by=self.leader)
        before = StockMovement.objects.count()
        result = subprocess.run(
            ['node', str(settings.BASE_DIR / 'tests/warehouse-actions-browser.cjs')],
            env=os.environ | {'ARMADA_TEST_URL': self.live_server_url, 'WAREHOUSE_ACTIONS_FIXTURE': json.dumps({
                'warehouse': str(warehouse.pk), 'first': str(self.obj.pk), 'second': str(other.pk),
                'private_name': other.name,
            })}, capture_output=True, text=True, timeout=150,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        warehouse.refresh_from_db()
        self.assertEqual(set(warehouse.served_objects.values_list('pk', flat=True)), {self.obj.pk, other.pk})
        self.assertEqual(StockMovement.objects.count(), before)
        self.assertEqual(StockBalance.objects.get(warehouse=warehouse, item=item).quantity, Decimal(10))
