import io
import json
import secrets
import uuid
import tempfile
from datetime import timedelta
from unittest.mock import patch, MagicMock
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from projects import test_stock as fixture
from repairs.models import Job, Defect, JobDefect, Report, Attachment
from repairs.review_services import return_for_revision
from . import links, dialog, worker, transport
from .models import Binding, LinkTicket, Inbox, Outbox, Draft, Photo, RateBucket

CONFIG = dict(ARMADA_MAX_ENABLED=True, ARMADA_MAX_WEBHOOK_SECRET='SYNTHETIC_WEBHOOK_SECRET_1234567890',
              ARMADA_MAX_TOKEN='SYNTHETIC_BOT_TOKEN', ARMADA_MAX_BOT_NAME='synthetic_bot')


@override_settings(**CONFIG)
class MaxTests(TestCase):
    setUpTestData = classmethod(fixture.StockTests.setUpTestData.__func__)

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.storage = override_settings(PRIVATE_DOCUMENT_ROOT=self.root.name)
        self.storage.enable()
        self.addCleanup(self.storage.disable)
        self.job = Job.objects.create(contract=self.contract, section=self.section, method='grind',
                    assigned_to=self.worker, created_by=self.leader)
        defect = Defect.objects.create(contract=self.contract, section=self.section, kilometre=1, number='ТЕСТ125')
        JobDefect.objects.create(job=self.job, defect=defect)
        self.binding = self.bind()
        Outbox.objects.all().delete()

    def bind(self, sender=101, chat=201, user=None):
        user = user or self.worker
        code = links.issue(user)
        with transaction.atomic():
            links.request_link(links.digest(code), sender, chat)
        return links.approve(user, links.digest(code))

    def event(self, text='', photos=None, sender=101, chat=201, **extra):
        row = Inbox.objects.create(key=uuid.uuid4().hex, max_user=sender, chat=chat,
                    payload={'kind': 'message_created', 'text': text, 'photos': photos or [], **extra})
        worker.process(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.state, 'done')
        return row

    def draft(self, job=None, action='repaired'):
        return dialog.start(self.binding, job or self.job, action)

    def fill(self, draft, values=None):
        values = values or {}
        for name in draft.fields:
            form = dialog.form_for(draft, self.worker)
            field = form.fields[name]
            value = values.get(name, {'performed_date': str(timezone.localdate()), 'coordinates': '55.123456, 73.123456',
                                      'note': 'ТЕСТ результат', 'problem': 'water', 'length_m': '1.5',
                                      'manual_serials': 'ТЕСТ-001'}.get(name, ''))
            self.event(str(value))
            draft.refresh_from_db()
        self.assertEqual(draft.state, 'photos')
        return draft

    def reviewed(self):
        draft = self.fill(self.draft())
        self.event('review:' + str(draft.pk))
        draft.refresh_from_db()
        self.assertEqual(draft.state, 'confirm')
        return draft

    def webhook(self, data, secret=CONFIG['ARMADA_MAX_WEBHOOK_SECRET']):
        return self.client.post(reverse('max_webhook'), json.dumps(data), content_type='application/json',
                                HTTP_X_MAX_BOT_API_SECRET=secret)

    def message(self, mid='test-mid', **changes):
        return {'update_type': 'message_created', 'message': {'sender': {'user_id': 101},
            'timestamp': 1000, 'recipient': {'chat_id': 201, 'chat_type': 'dialog'}, 'body': {'mid': mid, 'text': '/работы', **changes}}}

    def test_webhook_auth_and_disabled_fail_closed(self):
        self.assertEqual(self.webhook(self.message(), secret='bad').status_code, 403)
        with override_settings(ARMADA_MAX_ENABLED=False):
            self.assertEqual(self.webhook(self.message()).status_code, 503)
        self.assertFalse(Inbox.objects.exists())

    def test_group_and_malformed_events_are_rejected(self):
        data = self.message(); data['message']['recipient']['chat_type'] = 'chat'
        self.assertEqual(self.webhook(data).status_code, 400)
        for data in ([], {}, {'update_type': 'message_callback', 'message': None}, self.message(text=['bad'])):
            response = self.webhook(data)
            self.assertIn(response.status_code, (200, 400))
        self.assertFalse(Inbox.objects.exists())

    def test_webhook_dedup_and_message_persistence(self):
        self.assertEqual(self.webhook(self.message()).status_code, 200)
        self.assertEqual(self.webhook(self.message()).status_code, 200)
        self.assertEqual(Inbox.objects.count(), 1)
        with patch('maxbot.transport.send') as send:
            worker.tick()
            send.assert_not_called()
        row = Inbox.objects.get()
        self.assertEqual(row.payload, {})
        self.assertEqual(Outbox.objects.count(), 1)
        self.assertIn('ТЕСТ', Outbox.objects.get().body['attachments'][0]['payload']['buttons'][0][0]['text'])

    def test_link_code_not_stored_in_inbox_and_requires_cabinet_approval(self):
        links.revoke(self.worker)
        code = links.issue(self.worker)
        data = {'update_type': 'bot_started', 'user': {'user_id': 102}, 'chat_id': 202, 'payload': code, 'timestamp': 123}
        self.assertEqual(self.webhook(data).status_code, 200)
        self.assertNotIn(code, json.dumps(Inbox.objects.get().payload))
        worker.tick()
        self.assertFalse(Binding.objects.filter(active=True).exists())
        links.approve(self.worker, links.digest(code))
        self.assertTrue(Binding.objects.filter(active=True, max_user=102).exists())
        with transaction.atomic(), self.assertRaises(ValidationError):
            links.request_link(links.digest(code), 103, 203)

    def test_foreign_user_cannot_approve_or_link(self):
        code = links.issue(self.worker)
        with self.assertRaises(PermissionDenied):
            links.approve(self.supply, links.digest(code))

    def test_profile_csrf_and_owner(self):
        self.client.force_login(self.worker)
        response = self.client.get(reverse('max_connection'))
        self.assertContains(response, 'MAX подключён')
        secure_client = Client(enforce_csrf_checks=True)
        secure_client.force_login(self.worker)
        self.assertEqual(secure_client.post(reverse('max_connection'), {'action': 'revoke'}).status_code, 403)
        self.client.force_login(self.supply)
        self.assertEqual(self.client.get(reverse('max_connection')).status_code, 403)

    def test_profile_issue_approve_and_revoke(self):
        self.client.force_login(self.worker)
        response = self.client.post(reverse('max_connection'), {'action': 'issue'})
        self.assertContains(response, 'https://max.ru/synthetic_bot?start=')
        ticket = LinkTicket.objects.get(consumed=False)
        ticket.max_user = 102; ticket.chat = 202; ticket.save()
        response = self.client.post(reverse('max_connection'), {'action': 'approve', 'ticket': ticket.pk})
        self.assertEqual(response.status_code, 302)
        self.client.post(reverse('max_connection'), {'action': 'revoke'})
        self.assertFalse(Binding.objects.filter(active=True).exists())

    def test_full_report_survives_worker_restarts_and_is_idempotent(self):
        draft = self.reviewed()
        self.assertFalse(Report.objects.exists())
        self.event('confirm:' + str(draft.pk))
        # No process-local state: a fresh event resolves the same database draft.
        self.event('confirm:' + str(draft.pk))
        self.assertEqual(Report.objects.count(), 1)
        self.job.refresh_from_db(); self.assertEqual(self.job.state, 'reported')
        self.assertEqual(Report.objects.get().author, self.worker)

    def test_problem_uses_unrepaired_semantics(self):
        draft = self.fill(self.draft(action='unrepaired'), {'problem': 'access'})
        self.event('review:' + str(draft.pk)); self.event('confirm:' + str(draft.pk))
        self.job.refresh_from_db()
        self.assertEqual(self.job.problem, 'access'); self.assertEqual(self.job.state, 'progress')

    def test_max_date_prompt_default_and_summary_use_short_date(self):
        draft = self.draft()
        event = self.event('/неизвестно')
        # An ordinary message on the date step produces a validation error.
        dialog.prompt(event, self.binding, draft)
        body = Outbox.objects.filter(key=f'in:{event.pk}:reply').get().body
        self.assertIn('ДД.ММ.ГГ', body['text'])
        self.assertIn(timezone.localdate().strftime('%d.%m.%y'),
                      body['attachments'][0]['payload']['buttons'][0][0]['text'])
        self.fill(draft, {'performed_date': timezone.localdate().strftime('%d.%m.%y')})
        review_event = self.event('review:' + str(draft.pk))
        draft.refresh_from_db()
        self.assertEqual(draft.raw['performed_date'], timezone.localdate().isoformat())
        self.assertEqual(draft.state, 'confirm')
        self.assertIn('Дата: ' + timezone.localdate().strftime('%d.%m.%y'),
                      Outbox.objects.get(key=f'in:{review_event.pk}:reply').body['text'])
        self.event('confirm:' + str(draft.pk))
        self.assertEqual(Report.objects.get().performed_date, timezone.localdate())

    def test_invalid_dates_do_not_advance_and_century_is_explicit(self):
        draft = self.draft()
        for value in ('31.02.26', '11.11.2026', '01.01.99'):
            self.event(value)
            draft.refresh_from_db()
            self.assertEqual(draft.step, 0)
        self.assertEqual(dialog.report_date('29.02.24').isoformat(), '2024-02-29')
        self.assertEqual(dialog.report_date('01.01.69').year, 2069)

    def test_result_skips_note_and_coordinates_explain_message_input(self):
        draft = self.draft()
        self.assertNotIn('note', draft.fields)
        self.event(timezone.localdate().strftime('%d.%m.%y'))
        draft.refresh_from_db()
        self.assertEqual(draft.fields[draft.step], 'coordinates')
        body = Outbox.objects.latest('pk').body
        self.assertIn('Вставьте координаты из Google карт или Яндекс карт.', body['text'])
        self.assertIn('обычным сообщением', body['text'])
        self.assertNotIn('56.123456', body['text'])
        self.assertNotIn('attachments', body)
        self.event('55.123456, 73.123456')
        draft.refresh_from_db()
        self.assertEqual(draft.raw['coordinates'], '55.123456, 73.123456')
        self.assertTrue(links.current(self.binding))

    def test_problem_note_only_for_reasons_requiring_details(self):
        for reason in ('access', 'other', 'material_other', 'existing_coupling', 'oversize_p1'):
            draft = self.draft(action='unrepaired')
            self.event(timezone.localdate().strftime('%d.%m.%y'))
            self.event(reason)
            draft.refresh_from_db()
            if reason == 'access':
                self.assertNotIn('note', draft.fields)
                continue
            self.assertEqual(draft.fields[draft.step], 'note')
            step = draft.step
            self.event(' ')
            draft.refresh_from_db()
            self.assertEqual(draft.step, step)
            self.event('ТЕСТ уточнение причины')
            draft.refresh_from_db()
            self.assertEqual(draft.raw['note'], 'ТЕСТ уточнение причины')
            self.assertEqual(draft.fields[draft.step], 'coordinates')

    def test_problem_details_can_be_reviewed_and_removed_when_reason_changes(self):
        draft = self.draft(action='unrepaired')
        self.event(timezone.localdate().strftime('%d.%m.%y'))
        self.event('other')
        self.event('ТЕСТ причина')
        self.event('55.123456, 73.123456')
        self.event('review:' + str(draft.pk))
        draft.refresh_from_db()
        self.assertEqual(draft.state, 'confirm')
        self.event('edit:' + str(draft.pk))
        self.event(timezone.localdate().strftime('%d.%m.%y'))
        self.event('access')
        draft.refresh_from_db()
        self.assertNotIn('note', draft.fields)
        self.assertNotIn('note', draft.raw)
        self.event('55.123456, 73.123456')
        self.event('review:' + str(draft.pk))
        self.event('confirm:' + str(draft.pk))
        self.assertEqual(Report.objects.get().problem, 'access')

    def test_confirm_requires_summary_and_wrong_chat_cannot_submit(self):
        draft = self.draft()
        self.event('confirm:' + str(draft.pk))
        self.event('confirm:' + str(draft.pk), chat=202)
        self.assertFalse(Report.objects.exists())

    def test_revoke_and_relink_cancel_old_drafts_and_delivery(self):
        draft = self.reviewed()
        row = Outbox.objects.order_by('-created_at').first()
        links.revoke(self.worker); self.binding = self.bind()
        self.event('confirm:' + str(draft.pk))
        with patch('maxbot.transport.send') as send:
            worker.deliver(row.pk)
            send.assert_not_called()
        self.assertFalse(Report.objects.exists())

    def test_session_version_and_assignment_changes_stop_submission(self):
        draft = self.reviewed()
        Job.objects.filter(pk=self.job.pk).update(assigned_to=self.supply)
        self.event('confirm:' + str(draft.pk))
        Job.objects.filter(pk=self.job.pk).update(assigned_to=self.worker)
        type(self.worker).objects.filter(pk=self.worker.pk).update(session_version=self.worker.session_version + 1)
        self.event('confirm:' + str(draft.pk))
        self.assertFalse(Report.objects.exists())

    def test_conflict_preserves_draft_and_does_not_save_report(self):
        draft = self.reviewed()
        Job.objects.filter(pk=self.job.pk).update(version=self.job.version + 1)
        self.event('confirm:' + str(draft.pk))
        draft.refresh_from_db(); self.assertEqual(draft.state, 'confirm')
        self.assertFalse(Report.objects.exists())

    def test_out_of_order_answer_does_not_advance(self):
        draft = self.draft()
        self.event(f'answer:{draft.pk}:9:bad')
        draft.refresh_from_db(); self.assertEqual(draft.step, 0)

    def test_expiry_and_cancellation_preserve_no_report(self):
        draft = self.reviewed()
        Draft.objects.filter(pk=draft.pk).update(expires=timezone.now()-timedelta(seconds=1))
        self.event('confirm:' + str(draft.pk))
        self.event('/отмена')
        self.assertFalse(Report.objects.exists())

    def test_unknown_job_cannot_be_selected(self):
        Job.objects.filter(pk=self.job.pk).update(assigned_to=self.supply)
        self.event('job:' + str(self.job.pk))
        self.assertFalse(Draft.objects.exists())

    def test_invalid_coordinates_do_not_bypass_standard_form(self):
        draft = self.fill(self.draft(), {'coordinates': 'not coordinates'})
        self.event('review:' + str(draft.pk))
        draft.refresh_from_db(); self.assertEqual(draft.state, 'photos')
        self.assertFalse(Report.objects.exists())

    @staticmethod
    def image():
        output = io.BytesIO(); Image.new('RGB', (2, 2), 'white').save(output, format='PNG')
        return SimpleUploadedFile('photo.png', output.getvalue(), content_type='image/png')

    def test_photo_persists_then_copies_to_standard_private_attachment(self):
        draft = self.fill(self.draft())
        with patch('maxbot.dialog.download_photo', return_value=self.image()):
            self.event(photos=[{'type': 'image', 'url': 'https://synthetic.invalid/photo'}])
        self.assertEqual(Photo.objects.count(), 1)
        self.event('review:' + str(draft.pk)); self.event('confirm:' + str(draft.pk))
        self.assertEqual(Attachment.objects.count(), 1)
        self.assertEqual(Attachment.objects.get().sha256, Photo.objects.get().sha256)

    def test_photo_source_restriction_prevents_request(self):
        for url in ('http://127.0.0.1/x', 'https://evil.invalid/x', 'https://user:pass@cdn.invalid/x'):
            with patch('maxbot.transport.build_opener') as opener, self.assertRaises(ValidationError):
                transport.download_photo(url)
            opener.assert_not_called()

    @override_settings(ARMADA_MAX_PHOTO_HOSTS=('cdn.invalid',))
    def test_photo_private_dns_and_redirects_are_rejected(self):
        addresses = [(2, 1, 6, '', ('127.0.0.1', 443))]
        with patch('maxbot.transport.socket.getaddrinfo', return_value=addresses), self.assertRaises(ValidationError):
            transport.download_photo('https://cdn.invalid/image')
        self.assertIsNone(transport.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.invalid'))

    def test_delivery_claims_once_and_unknown_is_not_retried(self):
        self.event('/работы'); row = Outbox.objects.get()
        with patch('maxbot.transport.send', side_effect=transport.DeliveryError()) as send:
            worker.deliver(row.pk); worker.deliver(row.pk)
            self.assertEqual(send.call_count, 1)
        row.refresh_from_db(); self.assertEqual(row.state, 'unknown')

    def test_rate_limit_and_size_limit(self):
        minute = int(timezone.now().timestamp()) // 60
        RateBucket.objects.create(key=f'101:{minute}', count=30)
        self.assertEqual(self.webhook(self.message()).status_code, 429)
        response = self.client.post(reverse('max_webhook'), 'x' * 65537, content_type='application/json',
                                    HTTP_X_MAX_BOT_API_SECRET=CONFIG['ARMADA_MAX_WEBHOOK_SECRET'])
        self.assertEqual(response.status_code, 413)

    def test_revision_queues_only_after_decision_and_rolls_back_with_it(self):
        draft = self.reviewed(); self.event('confirm:' + str(draft.pk))
        self.job.refresh_from_db()
        with self.assertRaises(ValueError), transaction.atomic():
            return_for_revision(self.leader, self.job.pk, self.job.version, 'ТЕСТ уточнить')
            raise ValueError
        self.assertFalse(Outbox.objects.filter(key__startswith='revision:').exists())
        return_for_revision(self.leader, self.job.pk, self.job.version, 'ТЕСТ уточнить')
        row = Outbox.objects.get(key__startswith='revision:')
        self.assertEqual(row.binding_id, self.binding.pk)
        self.assertIn(str(self.job.pk), json.dumps(row.body))

    def test_cleanup_only_expired_max_files_and_queue_counts(self):
        draft = self.fill(self.draft())
        with patch('maxbot.dialog.download_photo', return_value=self.image()):
            self.event(photos=[{'type': 'image', 'url': 'https://synthetic.invalid/photo'}])
        Draft.objects.filter(pk=draft.pk).update(expires=timezone.now()-timedelta(seconds=1))
        call_command('maintain_max', cleanup=True, stdout=io.StringIO())
        self.assertFalse(Photo.objects.exists())
        draft.refresh_from_db(); self.assertEqual(draft.raw, {})

    def test_setup_only_plans_without_explicit_apply(self):
        with patch('maxbot.management.commands.setup_max_webhook.api') as api:
            call_command('setup_max_webhook', stdout=io.StringIO())
            api.assert_not_called()

    def test_manual_p2_report_preserves_serials_and_actual_length(self):
        self.job.method = 'p2'; self.job.length_m = '1.000'; self.job.wall_mm = 8
        self.job.save()
        draft = self.fill(self.draft(), {'length_m': '1.500', 'composition': '', 'manual_serials': 'ТЕСТ-П2-1500'})
        self.event('review:' + str(draft.pk)); self.event('confirm:' + str(draft.pk))
        self.assertEqual(Report.objects.count(), 1)
        report = Report.objects.get()
        self.assertEqual(str(report.length_m), '1.500')
        self.assertEqual(report.context['installation']['pieces'][0]['serial'], 'ТЕСТ-П2-1500')

    def test_manual_composite_p1_preserves_each_number_and_length(self):
        self.job.method = 'p1'; self.job.length_m = '2.500'; self.job.wall_mm = 8
        self.job.save()
        draft = self.fill(self.draft(), {'length_m': '2.500', 'composition': '1.0+1.5',
                                        'manual_serials': 'ТЕСТ-П1-001\nТЕСТ-П1-002'})
        self.event('review:' + str(draft.pk)); self.event('confirm:' + str(draft.pk))
        report = Report.objects.get()
        self.assertEqual(report.context['installation']['pieces'], [
            {'serial': 'ТЕСТ-П1-001', 'length_mm': 1000},
            {'serial': 'ТЕСТ-П1-002', 'length_mm': 1500}])

    def test_delivery_rechecks_access_after_assignment_revoked(self):
        draft = self.reviewed()
        row = Outbox.objects.filter(job=self.job).order_by('-created_at').first()
        Job.objects.filter(pk=self.job.pk).update(assigned_to=self.supply)
        with patch('maxbot.transport.send') as send:
            worker.deliver(row.pk)
            send.assert_not_called()
        row.refresh_from_db(); self.assertEqual(row.state, 'cancelled')

    def test_rate_limited_delivery_is_delayed_and_failed_claim_is_not_replayed(self):
        self.event('/работы'); row = Outbox.objects.get()
        with patch('maxbot.transport.send', side_effect=transport.DeliveryError('pending')):
            worker.deliver(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.state, 'pending'); self.assertGreater(row.available_at, timezone.now())
        Outbox.objects.filter(pk=row.pk).update(state='sending', available_at=timezone.now()-timedelta(minutes=3))
        with patch('maxbot.transport.send') as send:
            worker.tick(deliver_messages=True)
            send.assert_not_called()
        row.refresh_from_db(); self.assertEqual(row.state, 'unknown')

    @override_settings(ARMADA_MAX_PHOTO_HOSTS=('cdn.invalid',))
    def test_photo_request_has_no_bot_credentials_and_rejects_invalid_bytes(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'bad png'
        opener = MagicMock(); opener.open.return_value = response
        addresses = [(2, 1, 6, '', ('93.184.216.34', 443))]
        with patch('maxbot.transport.socket.getaddrinfo', return_value=addresses), patch('maxbot.transport.build_opener', return_value=opener):
            with self.assertRaises(ValidationError):
                transport.download_photo('https://cdn.invalid/x')
        request = opener.open.call_args.args[0]
        self.assertNotIn('Authorization', request.headers)
        self.assertNotIn(CONFIG['ARMADA_MAX_TOKEN'], str(request.headers))

    def test_api_fixed_origin_auth_and_success_response(self):
        response = MagicMock(); response.__enter__.return_value = response
        response.read.return_value = b'{"message":{"body":{"mid":"synthetic"}}}'
        opener = MagicMock(); opener.open.return_value = response
        with patch('maxbot.transport.build_opener', return_value=opener):
            transport.send(201, {'text': 'ТЕСТ'})
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith(transport.API + '/messages?'))
        self.assertEqual(request.headers['Authorization'], CONFIG['ARMADA_MAX_TOKEN'])

    def test_callback_acknowledgement_is_durable_and_deduplicated(self):
        data = self.message()
        data['update_type'] = 'message_callback'
        data['callback'] = {'user': {'user_id': 101}, 'timestamp': 1000, 'callback_id': 'synthetic-callback', 'payload': '/работы'}
        self.assertEqual(self.webhook(data).status_code, 200)
        self.assertEqual(self.webhook(data).status_code, 200)
        worker.tick()
        self.assertEqual(Outbox.objects.filter(key__endswith=':ack').count(), 1)
        with patch('maxbot.transport.api', return_value={'success': True}) as api:
            transport.send(201, Outbox.objects.get(key__endswith=':ack').body)
        self.assertTrue(api.call_args.args[1].startswith('/answers?callback_id='))

    def test_setup_refuses_token_for_different_bot(self):
        from django.core.management.base import CommandError
        with patch('maxbot.management.commands.setup_max_webhook.api', return_value={'is_bot': True, 'username': 'other_bot'}) as api:
            with self.assertRaises(CommandError):
                call_command('setup_max_webhook', apply=True, stdout=io.StringIO())
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args, ('GET', '/me'))

    def test_reserved_stock_numbers_use_existing_installation_rules(self):
        from projects import stock_planning as planning
        from projects.models import StockItem, StockMovement
        self.job.method = 'p2'; self.job.length_m = '1.000'; self.job.wall_mm = 8; self.job.save()
        warehouse = fixture.StockTests.wh(self)
        planning.receive(self.leader, fixture.StockTests.data(self, kind='coupling', unit='pcs', method='p2',
            length_mm=1500, wall_mm=10), warehouse, 1, self.today, 'ТЕСТ поступление', uuid.uuid4())
        item = StockItem.objects.get()
        item = planning.reserve(self.leader, item.pk, item.version, self.job.pk)
        self.job.refresh_from_db()
        draft = self.draft()
        serial_field = 'serial_' + item.coupling_id.hex
        self.assertIn(serial_field, draft.fields)
        self.assertNotIn('length_m', draft.fields)
        self.fill(draft, {serial_field: 'ТЕСТ-ЗАВ-991'})
        self.assertTrue(any('Введите заводской номер ремонтной конструкции'
                            in row.body.get('text', '') for row in Outbox.objects.all()))
        self.event('review:' + str(draft.pk)); self.event('confirm:' + str(draft.pk))
        self.assertEqual(Report.objects.count(), 1)
        item.refresh_from_db()
        self.assertEqual(item.serial, 'ТЕСТ-ЗАВ-991')
        self.assertEqual(fixture.StockTests.balance(self, warehouse, item), 0)
        count = StockMovement.objects.count()
        self.event('confirm:' + str(draft.pk))
        self.assertEqual(StockMovement.objects.count(), count)

    def test_reserved_composite_p1_asks_for_each_piece_number(self):
        from projects import stock_planning as planning
        from projects.models import StockItem
        self.job.method = 'p1'; self.job.length_m = '2.500'; self.job.wall_mm = 8
        self.job.composition = '1.0+1.5'
        self.job.save()
        warehouse = fixture.StockTests.wh(self)
        values = {}
        expected = {}
        for length in (1000, 1500):
            planning.receive(self.leader, fixture.StockTests.data(self, kind='coupling', unit='pcs', method='p1',
                length_mm=length, wall_mm=8), warehouse, 1, self.today, 'ТЕСТ поступление', uuid.uuid4())
            item = StockItem.objects.get(length_mm=length)
            item = planning.reserve(self.leader, item.pk, item.version, self.job.pk)
            number = f'ТЕСТ-П1-{length}'
            values['serial_' + item.coupling_id.hex] = number
            expected[str(item.coupling_id)] = number
        self.job.refresh_from_db()
        draft = self.draft()
        self.assertEqual(set(name for name in draft.fields if name.startswith('serial_')), set(values))
        self.fill(draft, values)
        self.event('review:' + str(draft.pk)); self.event('confirm:' + str(draft.pk))
        report = Report.objects.get()
        self.assertEqual(report.context['installation']['serials'], expected)
        self.assertEqual(str(report.length_m), '2.500')

    def test_p1_actual_cleaning_length_is_preserved(self):
        self.section.diameter_mm = 720; self.section.save()
        self.job.method = 'p1'; self.job.length_m = '1.000'; self.job.wall_mm = 8; self.job.save()
        draft = self.draft()
        self.assertIn('actual_cleaning_length_m', draft.fields)
        self.fill(draft, {'actual_cleaning_length_m': '2.400', 'length_m': '1.000'})
        self.event('review:' + str(draft.pk)); self.event('confirm:' + str(draft.pk))
        self.assertEqual(Report.objects.count(), 1)
        self.assertEqual(Report.objects.get().context['actual_cleaning_length_m'], '2.400')

    def test_stale_revision_notice_is_cancelled_after_new_result(self):
        draft = self.reviewed(); self.event('confirm:' + str(draft.pk))
        self.job.refresh_from_db()
        return_for_revision(self.leader, self.job.pk, self.job.version, 'ТЕСТ уточнить')
        row = Outbox.objects.get(key__startswith='revision:')
        Job.objects.filter(pk=self.job.pk).update(state='reported', review_note='')
        with patch('maxbot.transport.send') as send:
            worker.deliver(row.pk)
            send.assert_not_called()
        row.refresh_from_db(); self.assertEqual(row.state, 'cancelled')

    def test_queued_menu_is_cancelled_if_section_access_is_revoked(self):
        from projects.models import ObjectAccess
        self.event('/работы'); row = Outbox.objects.get()
        ObjectAccess.objects.filter(user=self.worker).delete()
        with patch('maxbot.transport.send') as send:
            worker.deliver(row.pk)
            send.assert_not_called()
        row.refresh_from_db(); self.assertEqual(row.state, 'cancelled')

    def test_unsupported_webhook_content_type_is_rejected(self):
        response = self.client.post(reverse('max_webhook'), '{}', content_type='text/plain',
                        HTTP_X_MAX_BOT_API_SECRET=CONFIG['ARMADA_MAX_WEBHOOK_SECRET'])
        self.assertEqual(response.status_code, 415)

    def test_retry_after_header_delays_delivery(self):
        from urllib.error import HTTPError
        opener = MagicMock()
        opener.open.side_effect = HTTPError(transport.API, 429, 'rate limit', {'Retry-After': '600'}, None)
        with patch('maxbot.transport.build_opener', return_value=opener), self.assertRaises(transport.DeliveryError) as error:
            transport.send(201, {'text': 'ТЕСТ'})
        self.assertEqual(error.exception.state, 'pending')
        self.assertEqual(error.exception.retry_after, 600)
        self.event('/работы'); row = Outbox.objects.get()
        with patch('maxbot.transport.send', side_effect=transport.DeliveryError('pending', 600)):
            worker.deliver(row.pk)
        row.refresh_from_db()
        self.assertGreater(row.available_at, timezone.now() + timedelta(seconds=590))

    def test_late_text_cannot_fill_a_different_question(self):
        draft = self.draft()
        self.event(str(timezone.localdate()), timestamp=2000)
        draft.refresh_from_db(); self.assertEqual(draft.step, 1)
        self.event('ТЕСТ задержанное сообщение', timestamp=1999)
        draft.refresh_from_db(); self.assertEqual(draft.step, 1)
        self.assertNotIn('note', draft.raw)
