from unittest import skipUnless
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import uuid
from django.db import connection,connections,close_old_connections
from django.test import TransactionTestCase,override_settings
from django.core.exceptions import ValidationError
from accounts.models import User,AccessGrant
from projects.models import LegalEntity,COMPANY_TAX_ID,ObjectAccess,ContractAccess
from projects import test_stock as fixtures
from repairs.models import Report
from repairs import ddk as service
from . import tests as base,ddk,worker
from .models import Draft,Inbox
from . import test_ddk as native_cases

@override_settings(**base.CONFIG)
@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL locks')
class NativeDDKConcurrencyTests(TransactionTestCase):
    bind=base.MaxTests.bind;event=base.MaxTests.event
    current=native_cases.NativeDDKTests.current;begin=native_cases.NativeDDKTests.begin;fill=native_cases.NativeDDKTests.fill;method=native_cases.NativeDDKTests.method
    def setUp(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixtures.StockTests.setUpTestData.__func__(type(self));base.MaxTests.setUp(self)
        self.draft=self.fill();self.event(f'ddk:review:{self.draft.pk}');self.draft.refresh_from_db()
        self.payload=f'ddk:save:{self.draft.pk}:{ddk.signature(self.draft)}'
    def parallel(self,functions):
        barrier=Barrier(len(functions))
        def run(fn):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:return fn()
                except ValidationError:return False
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=len(functions)) as pool:return list(pool.map(run,functions))
    def inbox(self):return Inbox.objects.create(key=uuid.uuid4().hex,max_user=101,chat=201,payload={'kind':'message_callback','text':self.payload,'photos':[]})
    def test_two_confirmations_make_one_history_record(self):
        a=self.inbox();b=self.inbox()
        self.parallel([lambda:worker.process(a.pk),lambda:worker.process(b.pk)])
        self.assertEqual(Report.objects.filter(context__ddk=True).count(),1)
        self.draft.refresh_from_db();self.assertEqual(self.draft.state,'submitted')
        self.assertEqual(Inbox.objects.filter(pk__in=[a.pk,b.pk],state='done').count(),2)
    def test_lk_and_bot_confirmations_do_not_deadlock_or_overwrite(self):
        user=User.objects.create_user('+70000000004',full_name='ТЕСТ ПТО',role='pto',must_change_password=False)
        AccessGrant.objects.create(user=user,permission='repairs.prepare',granted_by=self.leader)
        ObjectAccess.objects.create(user=user,project_object=self.obj,granted_by=self.leader)
        ContractAccess.objects.create(user=user,contract=self.contract,granted_by=self.leader)
        event=self.inbox()
        def web():
            return service.change(user,self.job.pk,{'token':uuid.uuid4(),'expected_version':1,'method':'p2','length_m':'1.5','composition':'','wall_mm':8,'date':self.today,'reason':'ТЕСТ ДДК через ЛК','files':[]})
        self.parallel([lambda:worker.process(event.pk),web])
        self.assertEqual(Report.objects.filter(context__ddk=True).count(),1)
        self.job.refresh_from_db();self.assertIn(self.job.method,['p1','p2']);self.assertEqual(self.job.state,'planned')
