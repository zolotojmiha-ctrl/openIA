from datetime import datetime,time,timedelta
from io import StringIO
from unittest.mock import patch
from unittest import skipIf,skipUnless
from django.db import connection,connections,close_old_connections
from django.test import TestCase,TransactionTestCase,override_settings
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from accounts.models import User
from projects import test_stock as fixture,test_release624 as sample,rentals
from projects.models import RentalPeriod
from . import tests as base,rental_reminders as service,worker,links
from .models import Outbox,Inbox

@override_settings(**base.CONFIG)
class RentalReminderTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    crew=fixture.StockTests.crew;rental=sample.Release624Tests.rental
    bind=base.MaxTests.bind
    def setUp(self):
        self.hire=self.rental();self.binding=self.bind();Outbox.objects.all().delete()
        self.now=datetime.combine(self.today,time(20),tzinfo=service.OMSK)
    def rows(self):return Outbox.objects.filter(scope__rental_reminder__isnull=False)
    def test_20_omsk_not_server_timezone_and_no_backlog_spam(self):
        self.assertEqual(service.queue_due(self.now-timedelta(seconds=1)),0)
        self.assertEqual(service.queue_due(self.now),1);self.assertEqual(service.queue_due(self.now+timedelta(hours=1)),0)
        row=self.rows().get();self.assertIn(self.hire.placement.equipment.registration_number,row.body['text'])
        self.assertIn(self.today.strftime('%d.%m.%Y'),row.body['text']);self.assertNotIn('₽',row.body['text']);self.assertNotIn(self.hire.reference,row.body['text'])
        self.assertEqual(row.scope['rental_reminder']['date'],self.today.isoformat())
        self.assertFalse(self.hire.days.exists())
    def test_reminder_opens_today_even_when_old_days_are_missing(self):
        service.queue_due(self.now);row=self.rows().get()
        from urllib.parse import urlsplit,parse_qs
        url=row.body['attachments'][0]['payload']['buttons'][0][0]['url'];query=parse_qs(urlsplit(url).query)
        self.assertEqual(query['date'],[self.today.isoformat()])
        self.client.force_login(self.worker);response=self.client.get(urlsplit(url).path,{'rental':str(self.hire.pk),'date':self.today.isoformat()})
        self.assertEqual(response.status_code,200);self.assertEqual(response.context['day_form']['date'].value(),self.today)
        self.assertNotContains(response,'Взаиморасчёты')
        self.assertFalse(self.hire.days.exists())
    def test_recorded_day_including_explicit_zero_never_gets_reminder(self):
        rentals.save_day(self.worker,self.hire.pk,self.today,'external')
        self.assertEqual(service.queue_due(self.now),0)
    def test_late_message_cancelled_when_day_was_filled(self):
        service.queue_due(self.now);row=self.rows().get()
        with patch('maxbot.rental_reminders.timezone.now',return_value=self.now):self.assertTrue(worker.delivery_allowed(row))
        rentals.save_day(self.worker,self.hire.pk,self.today,'work')
        with patch('maxbot.rental_reminders.timezone.now',return_value=self.now):self.assertFalse(worker.delivery_allowed(row))
    def test_custody_with_current_crew_also_receives_without_duplicate_assignment(self):
        from projects.models import EquipmentCustody
        p=self.hire.placement;crew=p.crew_assignment
        p.crew_assignment=None;p.save(update_fields=['crew_assignment'])
        custody=EquipmentCustody.objects.create(equipment=p.equipment,brigade=crew.brigade,
            start_date=self.today,created_by=self.leader)
        self.assertEqual(service.queue_due(self.now),1)
        custody.end_date=self.today-timedelta(days=1);custody.start_date=custody.end_date;custody.save()
        self.assertFalse(service.delivery_allowed(self.worker,self.rows().get().scope,self.now))

    def test_closed_or_ended_placement_and_no_responsibility_exclude_notifications(self):
        p=self.hire.placement;p.end_date=self.today-timedelta(days=1);p.save(update_fields=['end_date'])
        self.assertEqual(service.queue_due(self.now),0)
        p.end_date=None;p.save(update_fields=['end_date']);crew=p.crew_assignment
        crew.master=self.leader;crew.save(update_fields=['master'])
        self.assertEqual(service.queue_due(self.now),0)
    def test_archived_object_and_closed_accounting_period_exclude_notifications(self):
        p=self.hire.placement;p.project_object.status='archived';p.project_object.save()
        self.assertEqual(service.queue_due(self.now),0)
        p.project_object.status='active';p.project_object.save()
        RentalPeriod.objects.create(rental=self.hire,starts_on=self.today,ends_on=self.today,snapshot={},created_by=self.leader)
        self.assertEqual(service.queue_due(self.now),0)
    def test_inactive_or_stale_max_binding_and_other_roles_never_receive(self):
        self.worker.session_version+=1;self.worker.save(update_fields=['session_version'])
        self.assertEqual(service.queue_due(self.now),0)
        self.binding.session_version=self.worker.session_version;self.binding.save(update_fields=['session_version'])
        self.worker.is_active=False;self.worker.save(update_fields=['is_active']);self.assertEqual(service.queue_due(self.now),0)
    def test_pending_old_binding_is_refreshed_but_sent_or_unknown_not_repeated(self):
        service.queue_due(self.now);row=self.rows().get();self.assertEqual(row.state,'pending')
        # Re-linking cancels the old unsent queue entry.
        new=self.bind();self.assertNotEqual(new.pk,self.binding.pk)
        self.assertEqual(service.queue_due(self.now),1);row.refresh_from_db();self.assertEqual(row.binding_id,new.pk)
        for state in ('sent','sending','unknown'):
            row.state=state;row.save(update_fields=['state']);self.bind()
            self.assertEqual(service.queue_due(self.now),0)
    def test_midnight_stale_reminders_and_permission_revocation_block_delivery(self):
        service.queue_due(self.now);row=self.rows().get()
        self.assertFalse(service.delivery_allowed(self.worker,row.scope,self.now+timedelta(days=1)))
        p=self.hire.placement;p.crew_assignment.master=self.leader;p.crew_assignment.save(update_fields=['master'])
        self.assertFalse(service.delivery_allowed(self.worker,row.scope,self.now))
    def test_explicit_date_must_be_within_responsibility_and_assignment(self):
        self.client.force_login(self.worker);url=reverse('equipment_detail',args=[self.hire.placement.equipment_id])
        for value in ('wrong',(self.today+timedelta(days=1)).isoformat(),(self.today-timedelta(days=30)).isoformat()):
            self.assertEqual(self.client.get(url,{'rental':str(self.hire.pk),'date':value}).status_code,404)
    def test_scheduler_failure_does_not_block_normal_bot_processing(self):
        event=Inbox.objects.create(key='TEST normal',max_user=self.binding.max_user,chat=self.binding.chat,payload={'kind':'message_created','text':'/работы','photos':[]})
        with patch.object(service,'_last_scan',None),patch.object(service,'queue_due',side_effect=RuntimeError('TEST')),self.assertLogs(service.logger,level='WARNING'):
            worker.tick()
        event.refresh_from_db();self.assertEqual(event.state,'done')
    @skipIf(connection.vendor=='postgresql','Read-only command is tested outside a write transaction below')
    def test_diagnostics_only_reads_and_does_not_queue(self):
        output=StringIO();call_command('max_rental_status',name='ТЕСТ Мастер',stdout=output)
        self.assertIn('Только чтение',output.getvalue());self.assertIn('20:00',output.getvalue())
        self.assertEqual(self.rows().count(),0);self.assertFalse(self.hire.days.exists())


@override_settings(**base.CONFIG)
@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL read-only transactions and row locks')
class RentalReminderPostgresTests(TransactionTestCase):
    crew=fixture.StockTests.crew;rental=sample.Release624Tests.rental
    bind=base.MaxTests.bind
    def setUp(self):
        from projects.models import LegalEntity,COMPANY_TAX_ID
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        self.hire=self.rental();self.binding=self.bind();Outbox.objects.all().delete()
        self.now=datetime.combine(self.today,time(20),tzinfo=service.OMSK)
    def test_two_workers_queue_exactly_one_reminder(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        barrier=threading.Barrier(2)
        def queue(_):
            close_old_connections()
            try:barrier.wait(timeout=10);return service.queue_due(self.now)
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sum(pool.map(queue,[0,1])),1)
        self.assertEqual(Outbox.objects.count(),1)
    def test_status_runs_read_only_and_does_not_queue_or_fill(self):
        out=StringIO();call_command('max_rental_status',name='ТЕСТ Мастер',stdout=out)
        self.assertIn('Только чтение',out.getvalue());self.assertIn('20:00',out.getvalue())
        self.assertFalse(Outbox.objects.exists());self.assertFalse(self.hire.days.exists())
