import json
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from projects import test_stock as fixture
from projects.models import Contract,ContractObject,ObjectAccess,ContractAccess
from accounts.models import AccessGrant
from repairs.models import Job,Defect,ImportBatch
from repairs import archive_import as service
from repairs.test_archive_import import plan_for,upload
from . import tests as base,worker
from .models import Outbox,Draft

@override_settings(**base.CONFIG)
class NativeArchiveTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    setUp_base=base.MaxTests.setUp;bind=base.MaxTests.bind;event=base.MaxTests.event
    def setUp(self):
        self.setUp_base()
        self.leader_binding=self.bind(501,601,user=self.leader)
        self.archive=Contract.objects.create(legal_entity=self.company,number=service.TITLE,subject='ТЕСТ',work_kinds=['repairs'],status='planned')
        ContractObject.objects.create(contract=self.archive,project_object=self.obj)
        self.plan=plan_for(self.archive,self.obj);Outbox.objects.all().delete()
    def lead(self,text='',photos=None):return self.event(text,photos,sender=501,chat=601)
    def reply(self,event):return Outbox.objects.get(key=f'in:{event.pk}:reply')
    def prepare(self):
        self.lead(f'archive:upload:{self.archive.pk}')
        with patch('maxbot.archive.download_file',side_effect=lambda *args:upload(self.plan)):
            event=self.lead(photos=[{'type':'file','url':'https://synthetic.invalid/test','name':'test-history.json'}])
        return ImportBatch.objects.get(),self.reply(event)
    def test_native_upload_preview_paginate_apply_and_repeat(self):
        event=self.lead('/импорт_архива');self.assertIn(f'archive:upload:{self.archive.pk}',str(self.reply(event).body))
        batch,row=self.prepare();self.assertFalse(Job.objects.filter(contract=self.archive).exists());self.assertTrue(worker.delivery_allowed(row))
        event=self.lead(f'archive:rows:{batch.pk}:0');self.assertIn('100.000',self.reply(event).body['text'])
        proof=self.lead(f'archive:proof:{batch.pk}:0:0');self.assertIn('ТЕСТ!D8',self.reply(proof).body['text'])
        self.assertFalse(any(b[0]['type']=='link' for b in self.reply(event).body['attachments'][0]['payload']['buttons']))
        self.lead(f'archive:apply:{batch.pk}:{batch.payload["digest"]}')
        self.assertEqual(Defect.objects.filter(contract=self.archive).count(),3);self.assertFalse(worker.delivery_allowed(row))
        event=self.lead(f'archive:apply:{batch.pk}:{batch.payload["digest"]}');self.assertIn('уже перенесён',self.reply(event).body['text'])
        self.assertEqual(Job.objects.filter(contract=self.archive).count(),2)
    def test_cancel_and_bad_file_keep_everything_in_bot(self):
        self.lead(f'archive:upload:{self.archive.pk}')
        with patch('maxbot.archive.download_file',return_value=SimpleUploadedFile('bad.json',b'{}')):
            event=self.lead(photos=[{'type':'file','url':'https://synthetic.invalid/a','name':'bad.json'}])
        self.assertIn('контрольную сумму',self.reply(event).body['text']);self.assertFalse(ImportBatch.objects.exists())
        self.lead('/отмена');self.leader_binding.refresh_from_db();self.assertNotIn('historical_upload',self.leader_binding.menu_scope)
        batch,_=self.prepare();self.lead('/отмена');batch.refresh_from_db();self.assertTrue(batch.payload['cancelled']);self.assertFalse(Job.objects.filter(contract=self.archive).exists())
    def test_master_cannot_import_and_acl_revocation_blocks_queued_read(self):
        event=self.event('/импорт_архива');self.assertIn('недоступно',self.reply(event).body['text'])
        batch,_=self.prepare();self.lead(f'archive:apply:{batch.pk}:{batch.payload["digest"]}')
        AccessGrant.objects.create(user=self.worker,permission='repairs.report_scope',granted_by=self.leader)
        ContractAccess.objects.create(user=self.worker,contract=self.archive,granted_by=self.leader)
        event=self.event('/архив');row=self.reply(event);self.assertTrue(worker.delivery_allowed(row))
        self.assertIn('archive:job:',str(row.body))
        ContractAccess.objects.filter(user=self.worker,contract=self.archive).delete();self.assertFalse(worker.delivery_allowed(row))
    def test_archive_search_does_not_steal_answers_after_other_action(self):
        self.event('archive:search:archive');self.event('/работы')
        self.binding.refresh_from_db();self.assertNotIn('archive_search',self.binding.menu_scope)
    def test_native_read_shows_unknown_date_and_no_repair_action(self):
        batch,_=self.prepare();self.lead(f'archive:apply:{batch.pk}:{batch.payload["digest"]}')
        job=Job.objects.filter(contract=self.archive).get(defects__number='901')
        event=self.lead(f'archive:job:{job.pk}');row=self.reply(event)
        self.assertIn('не указана',row.body['text']);self.assertIn('П1',row.body['text'])
        self.assertNotIn('begin:',str(row.body));self.assertTrue(worker.delivery_allowed(row))
    def test_cross_binding_cannot_confirm_foreign_preview(self):
        batch,_=self.prepare()
        self.event(f'archive:apply:{batch.pk}:{batch.payload["digest"]}')
        self.assertFalse(Job.objects.filter(contract=self.archive).exists())
    def test_active_repair_draft_prevents_starting_upload(self):
        from .dialog import start
        start(self.leader_binding,self.job,'unrepaired')
        event=self.lead(f'archive:upload:{self.archive.pk}');self.assertIn('Сначала',self.reply(event).body['text'])
        self.leader_binding.refresh_from_db();self.assertNotIn('historical_upload',self.leader_binding.menu_scope)
    def test_signed_webhook_file_and_duplicate_delivery_use_native_import(self):
        from .models import Inbox
        import uuid
        def post(text='',attachments=None):
            mid=uuid.uuid4().hex
            data={'update_type':'message_created','message':{'sender':{'user_id':501},'timestamp':1000,
                'recipient':{'chat_id':601,'chat_type':'dialog'},'body':{'mid':mid,'text':text,'attachments':attachments or []}}}
            for _ in range(2):
                response=self.client.post(reverse('max_webhook'),json.dumps(data),content_type='application/json',HTTP_X_MAX_BOT_API_SECRET=base.CONFIG['ARMADA_MAX_WEBHOOK_SECRET'])
                self.assertEqual(response.status_code,200)
            from .views import normalize
            row=Inbox.objects.get(key=normalize(data)[0]);worker.process(row.pk)
            row.refresh_from_db();self.assertEqual(row.state,'done')
            return row
        post(f'archive:upload:{self.archive.pk}')
        with patch('maxbot.archive.download_file',side_effect=lambda *args:upload(self.plan)):
            post(attachments=[{'type':'file','filename':'history.json','payload':{'url':'https://synthetic.invalid/file'}}])
        batch=ImportBatch.objects.get()
        post(f'archive:apply:{batch.pk}:{batch.payload["digest"]}')
        self.assertEqual(Defect.objects.filter(contract=self.archive).count(),3)
        job=Job.objects.filter(contract=self.archive).first()
        row=self.lead(f'archive:evidence:{job.pk}:0');self.assertIn('ТЕСТ!D8',self.reply(row).body['text'])
        self.assertTrue(worker.delivery_allowed(self.reply(row)))
