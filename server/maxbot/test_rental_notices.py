from datetime import datetime,time,timedelta
from unittest.mock import patch
from django.test import TestCase,override_settings
from accounts.models import User,AuditEvent
from projects import test_stock as fixture,test_release624 as sample,rentals,rental_owner
from projects.models import RentalAccess
from . import tests as base,worker,rental_notices as notices,rental_reminders as reminders,links,lessor
from .models import Outbox

@override_settings(**base.CONFIG)
class RentalNoticesTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    crew=fixture.StockTests.crew;rental=sample.Release624Tests.rental;bind=base.MaxTests.bind
    def setUp(self):
        self.hire=self.rental();self.leader_binding=self.bind(501,601,user=self.leader)
        self.master_binding=self.bind(101,201);Outbox.objects.all().delete()
        self.now=datetime.combine(self.today,time(21),tzinfo=reminders.OMSK)
    def test_leader_gets_new_day_and_correction_with_no_repeat_for_same_kind(self):
        rentals.save_day(self.worker,self.hire.pk,self.today,'work')
        first=Outbox.objects.get(scope__rental_event__isnull=False);self.assertIn('10 ч',first.body['text'])
        rentals.save_day(self.worker,self.hire.pk,self.today,'work');self.assertEqual(Outbox.objects.count(),1)
        day=self.hire.days.get();rentals.save_day(self.worker,self.hire.pk,self.today,'idle',version=day.version,note='ТЕСТ исправление')
        rows=list(Outbox.objects.filter(scope__rental_event__isnull=False).order_by('created_at'))
        self.assertEqual(len(rows),2);self.assertIn('10 → 4',rows[1].body['text']);self.assertFalse(worker.delivery_allowed(rows[0]));self.assertTrue(worker.delivery_allowed(rows[1]))
    def test_overdue_at_21_only_once_and_late_fill_cancels(self):
        self.assertEqual(notices.queue_overdue(self.now-timedelta(seconds=1)),0)
        self.assertEqual(notices.queue_overdue(self.now),1);self.assertEqual(notices.queue_overdue(self.now),0)
        row=Outbox.objects.get(scope__rental_overdue__isnull=False)
        with patch('maxbot.rental_notices.timezone.now',return_value=self.now):self.assertTrue(worker.delivery_allowed(row))
        rentals.save_day(self.worker,self.hire.pk,self.today,'external')
        with patch('maxbot.rental_notices.timezone.now',return_value=self.now):self.assertFalse(worker.delivery_allowed(row))
    def test_unlinked_foreman_still_escalates_to_leader_and_no_automatic_hours(self):
        links.deactivate(self.worker)
        self.assertEqual(notices.queue_overdue(self.now),1);self.assertFalse(self.hire.days.exists())
        self.assertEqual(notices.queue_overdue(self.now+timedelta(days=1)),1)
    def test_owner_binding_menu_and_day_notifications_are_scoped_and_revocable(self):
        owner=User.objects.create_user('+70000000004',role='lessor',full_name='ТЕСТ владелец',must_change_password=False)
        access=rental_owner.grant(self.leader,self.hire.pk,owner.pk,0,True,False)
        binding=self.bind(777,888,user=owner);Outbox.objects.all().delete()
        rentals.save_day(self.worker,self.hire.pk,self.today,'work')
        message=Outbox.objects.get(binding=binding,scope__rental_event__isnull=False)
        self.assertTrue(worker.delivery_allowed(message));self.assertNotIn('Начислено',message.body['text'])
        self.assertIn('/owner/',message.body['attachments'][0]['payload']['buttons'][0][0]['url'])
        rental_owner.grant(self.leader,self.hire.pk,owner.pk,access.version,False,False)
        self.assertFalse(worker.delivery_allowed(message));self.assertFalse(lessor.delivery_allowed(owner,{'rental':str(self.hire.pk)}))
    def test_dispute_and_resolution_go_to_leader_and_correct_owner(self):
        owner=User.objects.create_user('+70000000004',role='lessor',full_name='ТЕСТ владелец',must_change_password=False)
        access=rental_owner.grant(self.leader,self.hire.pk,owner.pk,0,True,False);binding=self.bind(777,888,user=owner);Outbox.objects.all().delete()
        import uuid
        issue=rental_owner.dispute(owner,access.pk,self.today,'ТЕСТ проверить',uuid.uuid4())
        self.assertEqual(Outbox.objects.filter(binding=self.leader_binding).count(),1)
        self.client.force_login(self.leader)
        from django.urls import reverse
        response=self.client.post(reverse('rental_dispute_resolve',args=[issue.pk]),{'resolution':'ТЕСТ сверено'})
        self.assertEqual(response.status_code,302)
        message=Outbox.objects.get(binding=binding);self.assertIn('ТЕСТ сверено',message.body['text']);self.assertTrue(worker.delivery_allowed(message))
