import uuid
from datetime import timedelta
from decimal import Decimal
from django.test import TestCase, override_settings
from django.db import transaction
from django.utils import timezone
from projects import test_stock as fixtures
from repairs.models import Coupling
from accounts.models import AuditEvent
from . import tests as base, worker, coupling_notices, demand_watch
from .models import Outbox


@override_settings(**base.CONFIG)
class CouplingNoticeTests(TestCase):
    setUpTestData = classmethod(fixtures.StockTests.setUpTestData.__func__)
    bind = base.MaxTests.bind
    wh = fixtures.StockTests.wh
    data = fixtures.StockTests.data
    item = fixtures.StockTests.item
    move = fixtures.StockTests.move
    concrete = fixtures.StockTests.concrete
    make_job = fixtures.StockTests.make_job
    report = fixtures.StockTests.report

    def setUp(self):
        self.binding = self.bind(501, 601, user=self.leader)
        Outbox.objects.all().delete()

    def prepare(self):
        coupling, item = self.concrete()
        warehouse = self.wh(project_object=self.obj)
        self.move(item, q=1, dst=warehouse)
        return self.make_job(), coupling, item, warehouse

    def notices(self):
        return Outbox.objects.filter(key__startswith='coupling-stock:')

    def test_actual_consumption_identity_and_retry(self):
        job, coupling, item, warehouse = self.prepare()
        self.assertFalse(self.notices().exists())
        token = uuid.uuid4()
        report = self.report(job, [coupling], token=token)
        notice = self.notices().get()
        for text in (coupling.serial, warehouse.name, item.sku, '1000 мм', 'Списана со склада'):
            self.assertIn(text, notice.body['text'])
        self.assertNotIn('Потребность', notice.body['text'])
        self.assertTrue(worker.delivery_allowed(notice))
        self.report(job, [coupling], token=token)
        event = AuditEvent.objects.get(action='repair_reported', details__report_id=str(report.pk))
        coupling_notices.queue_event(event)
        self.assertEqual(self.notices().count(), 1)
        # Confirmation does not undo a warehouse deduction.
        job.state = 'confirmed'; job.save()
        self.assertTrue(worker.delivery_allowed(notice))

    def test_correction_reports_return_and_blocks_superseded_notice(self):
        job, coupling, item, warehouse = self.prepare()
        self.report(job, [coupling])
        old = self.notices().get()
        job.refresh_from_db()
        self.report(job, [], action='correction')
        new = self.notices().exclude(pk=old.pk).get()
        self.assertIn('Возвращена на склад', new.body['text'])
        self.assertIn(coupling.serial, new.body['text'])
        self.assertFalse(worker.delivery_allowed(old))
        self.assertTrue(worker.delivery_allowed(new))

    def test_legacy_report_does_not_claim_stock_consumption(self):
        coupling = Coupling.objects.create(project_object=self.obj, method='p2', length_mm=1000,
                                          wall_mm=8, serial='ТЕСТ БЕЗ СКЛАДА')
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):self.report(self.make_job(), [coupling])
        self.assertFalse(self.notices().exists())

    def test_unchanged_correction_keeps_original_consumption_without_repeat(self):
        job, coupling, item, warehouse = self.prepare()
        self.report(job, [coupling])
        notice = self.notices().get()
        job.refresh_from_db()
        self.report(job, [coupling], action='correction')
        self.assertEqual(self.notices().count(), 1)
        self.assertTrue(worker.delivery_allowed(notice))

    def test_archived_contract_blocks_queued_notice(self):
        job, coupling, item, warehouse = self.prepare()
        self.report(job, [coupling])
        notice = self.notices().get()
        self.contract.status = 'archived'; self.contract.save()
        self.assertFalse(worker.delivery_allowed(notice))

    def test_foreman_does_not_receive_management_stock_notice(self):
        self.bind(502, 602, user=self.worker)
        Outbox.objects.all().delete()
        job, coupling, item, warehouse = self.prepare()
        self.report(job, [coupling])
        self.assertEqual(list(self.notices().values_list('binding__user_id', flat=True)), [self.leader.pk])

    def test_report_and_notices_rollback_together(self):
        job, coupling, item, warehouse = self.prepare()
        try:
            with transaction.atomic():
                self.report(job, [coupling])
                self.assertTrue(self.notices().exists())
                raise RuntimeError('rollback')
        except RuntimeError:
            pass
        self.assertFalse(self.notices().exists())

    def test_role_revocation_blocks_delivery(self):
        job, coupling, item, warehouse = self.prepare()
        self.report(job, [coupling])
        notice = self.notices().get()
        self.leader.leader_slot = None; self.leader.role = 'foreman'; self.leader.save()
        self.assertFalse(worker.delivery_allowed(notice))

    def test_old_aggregate_pending_is_cancelled_but_sent_history_remains(self):
        defaults = dict(binding=self.binding, max_user=501, chat=601,
                        available_at=timezone.now(), expires=timezone.now() + timedelta(days=1),
                        body={'text': 'Потребность в муфтах изменилась'}, scope={'leader': True, 'demand_digest': 'old'})
        pending = Outbox.objects.create(key='shortage:old:1', **defaults)
        sent = Outbox.objects.create(key='shortage:old:2', state='sent', **defaults)
        self.assertFalse(worker.delivery_allowed(pending))
        demand_watch.queue_changes()
        pending.refresh_from_db(); sent.refresh_from_db()
        self.assertEqual(pending.state, 'cancelled')
        self.assertEqual(sent.state, 'sent')

    def test_ddk_reserves_free_p1_then_report_consumes_that_exact_item(self):
        from repairs import ddk
        from repairs.services import report_work
        from projects.models import StockMovement
        job = self.make_job()
        job.method = 'grind'; job.length_m = None; job.wall_mm = None; job.save()
        coupling = Coupling.objects.create(project_object=self.obj, method='p1', length_mm=2000,
                                          wall_mm=8, serial='ТЕСТ СВОБОДНАЯ П1')
        item = self.item(kind='coupling', unit='pcs', method='p1', length_mm=2000, wall_mm=8,
                         coupling=coupling, individual=True, serial=coupling.serial)
        warehouse = self.wh(project_object=self.obj)
        self.move(item, q=1, dst=warehouse)
        ddk.change(self.leader, job.pk, {'token': uuid.uuid4(), 'expected_version': job.version,
            'method': 'p1', 'length_m': Decimal('2'), 'composition': '2', 'wall_mm': Decimal('8'),
            'date': self.today, 'reason': 'ТЕСТ заключение', 'files': []})
        item.refresh_from_db(); job.refresh_from_db()
        self.assertEqual(item.reserved_job_id, job.pk)
        self.assertFalse(StockMovement.objects.filter(kind='consume').exists())
        self.assertFalse(self.notices().exists())
        report = report_work(self.worker, job.pk, 'result', {'token': uuid.uuid4(),
            'expected_version': job.version, 'performed_date': self.today, 'outcome': 'complete',
            'method': 'p1', 'length_m': Decimal('2'), 'composition': '2', 'note': 'ТЕСТ результат',
            'latitude': Decimal(55), 'longitude': Decimal(73), 'files': [], 'couplings': [coupling], 'installation': {
                'kind': 'reserved', 'serials': {str(coupling.pk): coupling.serial}}})
        movement = StockMovement.objects.get(kind='consume')
        self.assertEqual((movement.item_id, movement.report_id), (item.pk, report.pk))
        notice = self.notices().get()
        self.assertIn('2000 мм', notice.body['text'])
        self.assertIn(coupling.serial, notice.body['text'])
        self.assertTrue(worker.delivery_allowed(notice))
