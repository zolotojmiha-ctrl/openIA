from datetime import datetime,time
from decimal import Decimal
from unittest import skipUnless
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import uuid
from django.db import connection,connections,close_old_connections
from django.test import TransactionTestCase,override_settings
from django.core.exceptions import ValidationError
from accounts.models import User
from projects import test_stock as fixture,test_release624 as sample,rental_owner
from projects.models import LegalEntity,COMPANY_TAX_ID,RentalAccess
from repairs import ddk
from repairs.models import Report
from maxbot import tests as max_fixture,rental_notices,rental_reminders
from maxbot.models import Outbox

@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL row locks')
@override_settings(**max_fixture.CONFIG)
class RemainingConcurrencyTests(TransactionTestCase):
    crew=fixture.StockTests.crew;rental=sample.Release624Tests.rental;make_job=fixture.StockTests.make_job;bind=max_fixture.MaxTests.bind
    def setUp(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        self.hire=self.rental();self.job=self.make_job();self.job.method='grind';self.job.length_m=None;self.job.wall_mm=None;self.job.save()
        self.bind(501,601,user=self.leader);Outbox.objects.all().delete()
    def concurrent(self,fn):
        barrier=Barrier(2)
        def run(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:return fn(index)
                except ValidationError:return 0
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(run,[0,1]))
    def test_competing_ddk_submissions_do_not_overwrite_or_duplicate_history(self):
        def apply(index):
            ddk.change(self.worker,self.job.pk,{'token':uuid.uuid4(),'expected_version':1,'date':self.today,'method':'p1','length_m':Decimal('3'),'composition':'1.5+1.5','wall_mm':10,'reason':'ТЕСТ ДДК','files':[]})
            return 1
        self.assertEqual(sum(self.concurrent(apply)),1)
        self.assertEqual(Report.objects.filter(job=self.job,kind='plan_change').count(),1)
    def test_two_workers_create_one_overdue_notice(self):
        now=datetime.combine(self.today,time(21),tzinfo=rental_reminders.OMSK)
        self.assertEqual(sum(self.concurrent(lambda _:rental_notices.queue_overdue(now))),1)
        self.assertEqual(Outbox.objects.filter(scope__rental_overdue__isnull=False).count(),1)
    def test_access_grant_versions_reject_overlapping_admin_submissions(self):
        owner=User.objects.create_user('+70000000004',role='lessor',full_name='ТЕСТ владелец',must_change_password=False)
        def apply(index):rental_owner.grant(self.leader,self.hire.pk,owner.pk,0,True,bool(index));return 1
        self.assertEqual(sum(self.concurrent(apply)),1);self.assertEqual(RentalAccess.objects.count(),1)

    def test_diagnostics_run_read_only_without_queueing_or_ledger_writes(self):
        from django.core.management import call_command
        from io import StringIO
        output=StringIO();before=Outbox.objects.count()
        call_command('rental_collaboration_status',stdout=output)
        self.assertIn('Только чтение',output.getvalue());self.assertIn('21:00',output.getvalue())
        self.assertEqual(Outbox.objects.count(),before);self.assertFalse(self.hire.days.exists())
