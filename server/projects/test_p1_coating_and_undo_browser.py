import os
import secrets
import subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from accounts.models import AccessGrant, User
from . import test_stock as base, test_p1_coating_and_undo as feature
from .models import LegalEntity, COMPANY_TAX_ID, ObjectAccess, ContractAccess, StockBalance


class P1CoatingUndoBrowser(StaticLiveServerTestCase):
    wh=base.StockTests.wh
    data=base.StockTests.data
    item=base.StockTests.item
    move=base.StockTests.move
    make_job=base.StockTests.make_job
    job=feature.P1CoatingAndUndoTests.job
    supplies=feature.P1CoatingAndUndoTests.supplies
    pieces=feature.P1CoatingAndUndoTests.pieces
    send=feature.P1CoatingAndUndoTests.send
    deleted_kits=feature.P1CoatingAndUndoTests.deleted_kits

    def test_pto_reads_p1_norms_and_restores_deleted_24_kits(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        base.StockTests.setUpTestData.__func__(type(self))
        self.section.diameter_mm=720;self.section.save()
        password=secrets.token_urlsafe(24)
        pto=User.objects.create_user('+70000000739',password,role='pto',full_name='ТЕСТ ПТО',must_change_password=False)
        for permission in ['stock.manage','repairs.prepare']:
            AccessGrant.objects.create(user=pto,permission=permission,granted_by=self.leader)
        ObjectAccess.objects.create(user=pto,project_object=self.obj,granted_by=self.leader)
        ContractAccess.objects.create(user=pto,contract=self.contract,granted_by=self.leader)
        job=self.job();warehouse,items=self.supplies();pieces=self.pieces(warehouse);self.send(job,pieces)
        deleted_warehouse,item,loss=self.deleted_kits()
        env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,
                        'ARMADA_TEST_REPAIR':str(job.pk),'ARMADA_TEST_WAREHOUSE':str(deleted_warehouse.pk)}
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/p1-coating-undo-browser.cjs')],env=env,capture_output=True,text=True,timeout=150)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        item.refresh_from_db();self.assertIsNone(item.deleted_at);self.assertFalse(item.archived)
        self.assertEqual(StockBalance.objects.get(warehouse=deleted_warehouse,item=item).quantity,24)
        self.assertEqual(loss.reversal.quantity,24)
