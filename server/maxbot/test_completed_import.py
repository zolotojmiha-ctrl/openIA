import copy
from unittest.mock import patch
from django.test import TestCase,override_settings
from projects import test_stock as fixture
from repairs.models import ImportBatch,Job
from repairs.test_completed_import import plan_for,upload
from . import tests as base,worker
from .models import Outbox


@override_settings(**base.CONFIG)
class NativeCompletedImportTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    setUp_base=base.MaxTests.setUp;bind=base.MaxTests.bind;event=base.MaxTests.event
    def setUp(self):
        self.setUp_base();self.contract.number='ТЕСТ ТЗС-1731-2025';self.contract.status='active';self.contract.save()
        self.initial_count=Job.objects.count();self.leader_binding=self.bind(501,601,user=self.leader);self.plan=plan_for(self.contract,self.obj);Outbox.objects.all().delete()
    def lead(self,text='',photos=None):return self.event(text,photos,sender=501,chat=601)
    def reply(self,e):return Outbox.objects.get(key=f'in:{e.pk}:reply')
    def prepare(self):
        self.lead(f'ci:upload:{self.contract.pk}')
        with patch('maxbot.completed_import.download_file',return_value=upload(self.plan)):
            e=self.lead(photos=[{'type':'file','url':'https://example.invalid/private.json','name':'test.json'}])
        return ImportBatch.objects.get(),self.reply(e)
    def test_full_native_upload_preview_rows_proof_apply_repeat_and_read(self):
        e=self.lead('/импорт_2026');self.assertIn('ci:upload:',str(self.reply(e).body))
        batch,row=self.prepare();self.assertIn('Добавить устранённых: 2',row.body['text']);self.assertTrue(worker.delivery_allowed(row));self.assertEqual(Job.objects.count(),self.initial_count)
        e=self.lead(f'ci:rows:{batch.pk}:0');self.assertIn('№900',self.reply(e).body['text'])
        e=self.lead(f'ci:proof:{batch.pk}:0:0');self.assertIn('ТЕСТ!C8',self.reply(e).body['text'])
        e=self.lead(f'ci:apply:{batch.pk}:{batch.payload["digest"]}');self.assertIn('действующий контракт',self.reply(e).body['text']);self.assertEqual(Job.objects.count(),self.initial_count+2)
        self.lead(f'ci:apply:{batch.pk}:{batch.payload["digest"]}');self.assertEqual(Job.objects.count(),self.initial_count+2)
        e=self.lead('/устранённые');self.assertIn('archive:job:',str(self.reply(e).body))
        e=self.lead('archive:job:'+str(Job.objects.get(defects__number='901').pk));self.assertIn('не указана',self.reply(e).body['text']);self.assertIn('Источники и монтаж',str(self.reply(e).body))
    def test_cancel_invalid_upload_forbidden_user_and_delivery_revocation(self):
        e=self.event('/импорт_2026');self.assertIn('недоступен',self.reply(e).body['text']);self.assertFalse(ImportBatch.objects.exists())
        batch,row=self.prepare();self.lead('/отмена');self.assertFalse(worker.delivery_allowed(row))
        batch.refresh_from_db();self.assertTrue(batch.payload['cancelled']);self.assertEqual(Job.objects.count(),self.initial_count)
        self.leader_binding.refresh_from_db();self.assertNotIn('completed_upload',self.leader_binding.menu_scope)
        self.plan=copy.deepcopy(self.plan);self.plan['payload_sha256']='bad';self.lead(f'ci:upload:{self.contract.pk}')
        with patch('maxbot.completed_import.download_file',return_value=upload(self.plan)):
            e=self.lead(photos=[{'type':'file','url':'https://example.invalid/private.json','name':'test.json'}])
        self.assertIn('Проверьте',self.reply(e).body['text']);self.assertEqual(ImportBatch.objects.count(),1)
