import copy,tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from django.test import TransactionTestCase,override_settings
from django.db import connection,connections,close_old_connections
from django.core.exceptions import ValidationError
from accounts.models import User
from projects import test_stock as fixture
from projects.models import LegalEntity,COMPANY_TAX_ID
from .test_layouts import xlsx,ROWS
from .models import PipeLayout
from . import layouts

@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL locks')
class LayoutConcurrencyTests(TransactionTestCase):
    def setUp(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'});fixture.StockTests.setUpTestData.__func__(type(self))
        root=tempfile.TemporaryDirectory();self.addCleanup(root.cleanup)
        config=override_settings(PRIVATE_DOCUMENT_ROOT=root.name);config.enable();self.addCleanup(config.disable)
    def parallel(self,functions):
        barrier=Barrier(len(functions))
        def run(fn):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:fn();return 'ok'
                except ValidationError:return 'stale'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=len(functions)) as pool:return list(pool.map(run,functions))
    def test_two_confirmations_one_active_version(self):
        row=layouts.prepare(self.leader,self.section.pk,xlsx())
        self.assertEqual(self.parallel([lambda:layouts.apply(self.leader,row.pk) for _ in range(2)]),['ok','ok'])
        self.assertEqual(PipeLayout.objects.filter(state='active').count(),1)
    def test_two_different_previews_do_not_overwrite_the_winning_version(self):
        other=User.objects.create_user('+70000888811',full_name='ТЕСТ второй',leader_slot=2,must_change_password=False)
        a=layouts.prepare(self.leader,self.section.pk,xlsx());rows=copy.deepcopy(ROWS);rows[2][2]='21';b=layouts.prepare(other,self.section.pk,xlsx(rows))
        self.assertCountEqual(self.parallel([lambda:layouts.apply(self.leader,a.pk),lambda:layouts.apply(other,b.pk)]),['ok','stale'])
        self.assertEqual(PipeLayout.objects.filter(state='active').count(),1)
