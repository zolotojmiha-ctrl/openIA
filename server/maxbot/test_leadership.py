import secrets
from unittest.mock import patch
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from accounts.models import User
from accounts.views import audit
from projects import test_stock as fixture
from projects.models import MaterialRequest, Equipment
from . import leadership, links, worker
from .models import Binding, Outbox, Draft
from . import tests as base
from .tests import CONFIG


@override_settings(**CONFIG)
class LeaderTests(TestCase):
    setUpTestData = classmethod(fixture.StockTests.setUpTestData.__func__)
    setUp = base.MaxTests.setUp
    bind = base.MaxTests.bind
    event = base.MaxTests.event
    draft = base.MaxTests.draft
    fill = base.MaxTests.fill
    reviewed = base.MaxTests.reviewed

    def leader_binding(self):
        binding = self.bind(sender=301, chat=401, user=self.leader)
        Outbox.objects.all().delete()
        return binding

    def test_leader_can_connect_from_profile_and_get_role_menu(self):
        self.client.force_login(self.leader)
        self.assertContains(self.client.get(reverse('profile')), 'Подключение MAX')
        self.assertEqual(self.client.get(reverse('max_connection')).status_code, 200)
        binding = self.leader_binding()
        self.assertTrue(links.current(binding))
        self.event('/меню', sender=301, chat=401)
        row = Outbox.objects.get()
        self.assertIn('Меню руководителя', row.body['text'])
        titles = [b[0]['text'] for b in row.body['attachments'][0]['payload']['buttons']]
        self.assertIn('Отчёты на проверку', titles)
        self.assertIn('Техника и ремонты', titles)
        self.assertIn('Потребность в материалах', titles)
        self.event('begin:' + str(self.job.pk) + ':repaired', sender=301, chat=401)
        self.assertFalse(Draft.objects.filter(binding=binding).exists())

    def test_master_cannot_use_leader_callbacks_or_read_leader_notices(self):
        self.event('leader:reports:0')
        self.assertFalse(any(row.scope.get('leader') for row in Outbox.objects.all()))
        self.assertFalse(leadership.delivery_allowed(self.worker, {'leader': True}))

    def test_report_goes_to_both_connected_leaders_and_stale_notice_is_cancelled(self):
        self.leader_binding()
        second = User.objects.create_user('+70000999301', secrets.token_urlsafe(20),
            full_name='ТЕСТ руководитель', leader_slot=2, must_change_password=False)
        self.bind(sender=302, chat=402, user=second)
        draft = self.reviewed()
        self.event('confirm:' + str(draft.pk))
        rows = Outbox.objects.filter(key__startswith='leader:')
        self.assertEqual(rows.count(), 2)
        self.assertEqual(set(rows.values_list('binding__user_id', flat=True)), {self.leader.pk, second.pk})
        self.event('leader:reports:0', sender=301, chat=401)
        self.assertTrue(any(b[0].get('url', '').endswith(reverse('repair_detail', args=[self.job.pk]))
            for row in Outbox.objects.filter(binding__user=self.leader)
            for b in row.body.get('attachments', [{}])[0].get('payload', {}).get('buttons', [])))
        self.job.state = 'confirmed'; self.job.save()
        for row in rows:
            with patch('maxbot.transport.send') as send:
                worker.deliver(row.pk)
                send.assert_not_called()
            row.refresh_from_db(); self.assertEqual(row.state, 'cancelled')

    def test_material_notice_is_transactional_and_rechecks_status(self):
        self.leader_binding()
        item = MaterialRequest.objects.create(contract=self.contract, project_object=self.section.project_object,
            name='ТЕСТ материал', quantity=2, unit='шт', created_by=self.leader)
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                audit(self.leader, 'repair_prepared', kind='material_request', record=str(item.pk))
                self.assertEqual(Outbox.objects.filter(key__startswith='leader:').count(), 1)
                raise RuntimeError('ТЕСТ откат')
        self.assertFalse(Outbox.objects.filter(key__startswith='leader:').exists())
        audit(self.leader, 'repair_prepared', kind='material_request', record=str(item.pk))
        row = Outbox.objects.get(key__startswith='leader:')
        item.quantity = 3; item.save()
        audit(self.leader, 'repair_prepared', kind='material_request', record=str(item.pk))
        self.assertFalse(worker.delivery_allowed(row))
        row = Outbox.objects.filter(key__startswith='leader:').exclude(pk=row.pk).get()
        self.assertTrue(worker.delivery_allowed(row))
        item.status = 'provided'; item.save()
        self.assertFalse(worker.delivery_allowed(row))

    def test_leader_menu_and_delivery_blocked_after_role_loss(self):
        self.leader_binding()
        self.event('/меню', sender=301, chat=401)
        row = Outbox.objects.get()
        User.objects.filter(pk=self.leader.pk).update(leader_slot=None, role='foreman')
        self.assertFalse(worker.delivery_allowed(row))

    def test_equipment_notice_lists_only_current_repair(self):
        self.leader_binding()
        item = Equipment.objects.create(legal_entity=self.contract.legal_entity, name='ТЕСТ техника',
            category='ТЕСТ', inventory_number='ТЕСТ-MAX', status='repair', created_by=self.leader)
        audit(self.leader, 'equipment_updated', equipment=str(item.pk), previous={'status': 'ready'},
              values={'status': 'repair'}, version=item.version)
        row = Outbox.objects.get(key__startswith='leader:')
        self.assertTrue(worker.delivery_allowed(row))
        self.event('leader:equipment:0', sender=301, chat=401)
        item.status = 'ready'; item.save()
        self.assertFalse(worker.delivery_allowed(row))

    def test_other_employee_roles_cannot_connect(self):
        self.client.force_login(self.supply)
        self.assertEqual(self.client.get(reverse('max_connection')).status_code, 403)

    def test_first_password_change_offers_optional_connection_and_regular_change_does_not(self):
        password = secrets.token_urlsafe(20)
        self.worker.set_password(password)
        self.worker.must_change_password = True
        self.worker.save()
        self.client.force_login(self.worker)
        new = secrets.token_urlsafe(20)
        response = self.client.post(reverse('password'), {'old_password': password, 'new_password1': new, 'new_password2': new})
        self.assertRedirects(response, reverse('max_onboarding'))
        self.assertContains(self.client.get(reverse('max_onboarding')), 'Подключить MAX')
        self.assertRedirects(self.client.post(reverse('max_onboarding'), {'action': 'later'}), reverse('dashboard'))
        self.assertRedirects(self.client.get(reverse('max_onboarding')), reverse('dashboard'))
        next_password = secrets.token_urlsafe(20)
        response = self.client.post(reverse('password'), {'old_password': new, 'new_password1': next_password, 'new_password2': next_password})
        self.assertRedirects(response, reverse('dashboard'))

    def test_first_change_offer_can_continue_to_connection(self):
        self.client.force_login(self.leader)
        session = self.client.session
        session['max_onboarding'] = str(self.leader.pk)
        session.save()
        self.assertContains(self.client.get(reverse('max_onboarding')), 'меню руководителя')
        self.assertRedirects(self.client.post(reverse('max_onboarding'), {'action': 'connect'}), reverse('max_connection'))
