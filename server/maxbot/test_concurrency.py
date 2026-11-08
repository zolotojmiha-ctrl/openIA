import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from django.db import connection, connections, close_old_connections
from django.test import TransactionTestCase, Client, override_settings
from django.utils import timezone
from projects import test_stock as fixture
from projects.models import LegalEntity, COMPANY_TAX_ID
from repairs.models import Job, Report
from . import dialog, worker
from .models import Binding, Inbox, Draft
from .tests import CONFIG


@skipUnless(connection.vendor == 'postgresql', 'Row-lock concurrency requires PostgreSQL.')
@override_settings(**CONFIG)
class MaxConcurrency(TransactionTestCase):
    def setUp(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID, defaults={'name': 'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        self.job = Job.objects.create(contract=self.contract, section=self.section, method='grind',
                                     assigned_to=self.worker, created_by=self.leader)
        self.binding = Binding.objects.create(user=self.worker, max_user=101, chat=201,
                                              session_version=self.worker.session_version)

    def parallel(self, function, values):
        barrier = Barrier(len(values))
        def run(value):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return function(value)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=len(values)) as pool:
            return list(pool.map(run, values))

    def test_two_confirmations_create_one_report(self):
        draft = dialog.start(self.binding, self.job, 'repaired')
        draft.raw.update(performed_date=str(timezone.localdate()), note='ТЕСТ параллельный отчёт', coordinates='55, 73')
        draft.state = 'confirm'; draft.step = len(draft.fields); draft.save()
        keys = []
        for _ in range(2):
            event = Inbox.objects.create(key=uuid.uuid4().hex, max_user=101, chat=201,
                        payload={'kind': 'message_callback', 'text': 'confirm:' + str(draft.pk), 'photos': []})
            keys.append(event.pk)
        self.parallel(worker.process, keys)
        self.assertEqual(Report.objects.count(), 1)
        self.assertEqual(Inbox.objects.filter(state='done').count(), 2)
        self.job.refresh_from_db(); self.assertEqual(self.job.state, 'reported')

    def test_same_event_arriving_concurrently_is_stored_once(self):
        import json
        data = {'update_type': 'message_created', 'message': {'sender': {'user_id': 101},
                'timestamp': 1000,
                'recipient': {'chat_id': 201, 'chat_type': 'dialog'}, 'body': {'mid': 'same', 'text': '/работы'}}}
        def submit(_):
            return Client().post('/integrations/max/webhook/', json.dumps(data), content_type='application/json',
                    HTTP_X_MAX_BOT_API_SECRET=CONFIG['ARMADA_MAX_WEBHOOK_SECRET']).status_code
        self.assertEqual(self.parallel(submit, [1, 2]), [200, 200])
        self.assertEqual(Inbox.objects.count(), 1)
