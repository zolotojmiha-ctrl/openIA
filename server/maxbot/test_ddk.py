import io,json,tempfile,uuid
from datetime import timedelta
from unittest.mock import patch,MagicMock
from django.test import TestCase,override_settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from django.urls import reverse
from accounts.models import User,AccessGrant
from repairs.models import Report,Attachment
from repairs import plans
from . import tests as base,ddk,worker,links,transport,views
from .models import Draft,Outbox,Photo,Inbox

@override_settings(**base.CONFIG)
class NativeDDKTests(TestCase):
    setUpTestData=classmethod(base.MaxTests.setUpTestData.__func__)
    setUp=base.MaxTests.setUp
    bind=base.MaxTests.bind;event=base.MaxTests.event
    image=staticmethod(base.MaxTests.image)
    def current(self):return Draft.objects.filter(binding=self.binding,action='ddk').latest('created_at')
    def method(self,draft,value):
        draft.refresh_from_db();self.event(f'ddk:answer:{draft.pk}:0:{draft.raw["_ddk_question"]}:{value}')
    def save(self,draft):
        draft.refresh_from_db();self.event(f'ddk:save:{draft.pk}:{ddk.signature(draft)}')
    def begin(self):
        self.event(f'job:{self.job.pk}')
        row=Outbox.objects.filter(key__endswith=':reply').order_by('-created_at').first()
        buttons=row.body['attachments'][0]['payload']['buttons']
        target=next(b[0] for b in buttons if 'ДДК' in b[0]['text'])
        self.assertEqual(target['type'],'callback');self.event(target['payload']);return self.current()
    def fill(self,method='p1',composition='1,5+1,5'):
        draft=self.begin();self.method(draft,method)
        if method in ('p1','p2','p3','p4','p5u'):
            self.event('3')
            if method!='p2':self.event(composition)
            self.event('10')
        self.event(self.today.strftime('%d.%m.%Y'));self.event('ДДК: ТЕСТ требуется изменить метод')
        return self.current()
    def test_complete_flow_with_image_and_pdf_never_opens_lk_or_marks_complete(self):
        draft=self.fill()
        self.assertEqual(draft.state,'photos');self.assertFalse(Report.objects.exists())
        with patch('maxbot.ddk.download_photo',return_value=self.image()),patch('maxbot.ddk.download_file',return_value=SimpleUploadedFile('ddk.pdf',b'%PDF-TEST')):
            self.event(photos=[{'type':'image','url':'https://cdn.invalid/photo'},{'type':'file','url':'https://cdn.invalid/pdf','name':'ddk.pdf'}])
        self.event(f'ddk:review:{draft.pk}');draft.refresh_from_db();self.assertEqual(draft.state,'confirm')
        self.job.refresh_from_db();self.assertEqual(self.job.method,'grind')
        self.save(draft);self.job.refresh_from_db();draft.refresh_from_db()
        self.assertEqual((self.job.method,self.job.length_m,self.job.wall_mm),('p1',3,10));self.assertEqual(self.job.state,'planned')
        report=Report.objects.get();self.assertTrue(report.context['ddk']);self.assertEqual(report.context['previous_plan']['method'],'grind')
        self.assertEqual(report.attachments.count(),2);self.assertEqual(draft.state,'submitted')
        self.assertFalse(self.job.installed_couplings.exists());self.assertEqual(self.worker.grants.count(),0)
        before=Report.objects.count();self.save(draft);self.assertEqual(Report.objects.count(),before)
    def test_save_without_review_is_rejected_and_cancel_changes_nothing(self):
        draft=self.fill();self.save(draft);self.assertFalse(Report.objects.exists())
        self.event('/отмена');draft.refresh_from_db();self.assertEqual(draft.state,'cancelled')
        self.job.refresh_from_db();self.assertEqual(self.job.method,'grind')
        self.save(draft);self.assertFalse(Report.objects.exists())
    def test_non_coupling_skips_sizes_and_single_piece_p2_skips_composition(self):
        self.job.method='p1';self.job.length_m=3;self.job.wall_mm=8;self.job.save()
        draft=self.fill('grind');self.assertEqual(draft.fields,['method','date','reason'])
        self.event(f'ddk:review:{draft.pk}');self.save(draft)
        self.job.refresh_from_db();self.assertEqual(self.job.method,'grind');self.assertIsNone(self.job.length_m)
        draft=self.fill('p2');self.assertNotIn('composition',draft.fields)
        self.event(f'ddk:review:{draft.pk}');self.save(draft)
        self.job.refresh_from_db();self.assertEqual(self.job.method,'p2')
    def test_validation_error_keeps_answers_and_recovery_buttons_stay_in_bot(self):
        draft=self.fill(composition='1+1');self.event(f'ddk:review:{draft.pk}')
        self.assertFalse(Report.objects.exists());draft.refresh_from_db();self.assertEqual(draft.raw['composition'],'1+1')
        row=Outbox.objects.filter(key__endswith=':error').order_by('-created_at').first()
        buttons=row.body['attachments'][0]['payload']['buttons'];self.assertTrue(any(b[0].get('payload','').startswith('ddk:edit:') for b in buttons))
        self.event(f'ddk:edit:{draft.pk}');self.method(draft,'p1');self.event('3');self.event('1,5+1,5');self.event('10');self.event(str(self.today));self.event('ТЕСТ исправлен состав')
        self.event(f'ddk:review:{draft.pk}');self.save(draft);self.assertEqual(Report.objects.count(),1)
    def test_restart_and_stale_questions_do_not_replace_answers(self):
        draft=self.begin();self.method(draft,'p1');self.event('1,5')
        draft.refresh_from_db();self.assertEqual(draft.step,2)
        self.event(f'ddk:answer:{draft.pk}:0:obsolete:p2');draft.refresh_from_db();self.assertEqual(draft.raw['method'],'p1');self.assertEqual(draft.step,2)
        self.event('/ддк');draft.refresh_from_db();self.assertEqual(draft.raw['length_m'],'1.5')
        self.event(f'begin:{self.job.pk}:repaired');draft.refresh_from_db();self.assertEqual(draft.state,'editing')
        self.assertEqual(Draft.objects.filter(binding=self.binding,state__in=ddk.STATES).count(),1)
    def test_changed_job_requires_explicit_recheck_and_preserves_evidence(self):
        draft=self.fill()
        with patch('maxbot.ddk.download_file',return_value=SimpleUploadedFile('ddk.pdf',b'%PDF-TEST')):
            self.event(photos=[{'type':'file','url':'https://cdn.invalid/pdf','name':'ddk.pdf'}])
        self.job.version+=1;self.job.save(update_fields=['version'])
        self.event(f'ddk:review:{draft.pk}');self.assertFalse(Report.objects.exists());draft.refresh_from_db();self.assertEqual(draft.photos.count(),1)
        message=Outbox.objects.filter(scope__ddk_refresh=True).latest('created_at');self.assertTrue(worker.delivery_allowed(message))
        self.event(f'ddk:refresh:{draft.pk}');draft.refresh_from_db();self.assertEqual(draft.step,0);self.assertEqual(draft.photos.count(),1)
        self.assertEqual(draft.raw['expected_version'],self.job.version)
        self.assertFalse(worker.delivery_allowed(message))
    def test_permissions_revocation_blocks_existing_draft_and_queued_messages(self):
        draft=self.fill();self.event(f'ddk:review:{draft.pk}')
        message=Outbox.objects.filter(scope__ddk_state='confirm').latest('created_at');self.assertTrue(worker.delivery_allowed(message))
        self.job.assigned_to=self.leader;self.job.save(update_fields=['assigned_to'])
        self.assertFalse(worker.delivery_allowed(message));self.save(draft);self.assertFalse(Report.objects.exists())
    def test_summary_is_invalidated_if_files_or_answers_change(self):
        draft=self.fill();self.event(f'ddk:review:{draft.pk}')
        message=Outbox.objects.filter(scope__ddk_state='confirm').latest('created_at')
        self.event(f'ddk:files:{draft.pk}')
        with patch('maxbot.ddk.download_file',return_value=SimpleUploadedFile('ddk.pdf',b'%PDF-TEST')):
            self.event(photos=[{'type':'file','url':'https://cdn.invalid/pdf','name':'ddk.pdf'}])
        self.event(f'ddk:review:{draft.pk}');self.assertFalse(worker.delivery_allowed(message))
    def test_bad_upload_batch_rolls_back_temporary_records_and_files(self):
        draft=self.fill();before=list(__import__('pathlib').Path(self.root.name).rglob('*'))
        with patch('maxbot.ddk.download_file',side_effect=[SimpleUploadedFile('ddk.txt',b'TEST'),ValidationError('ТЕСТ загрузка не удалась')]):
            self.event(photos=[{'type':'file','url':'https://cdn.invalid/1','name':'ddk.txt'},{'type':'file','url':'https://cdn.invalid/2','name':'bad.pdf'}])
        self.assertEqual(Photo.objects.filter(draft=draft).count(),0)
        self.assertFalse([p for p in __import__('pathlib').Path(self.root.name).rglob('*') if p.is_file()])
    def test_event_replay_has_one_draft_and_no_duplication(self):
        row=Inbox.objects.create(key='TEST-replay-ddk',max_user=101,chat=201,payload={'kind':'message_callback','text':f'ddk:select:{self.job.pk}','photos':[]})
        worker.process(row.pk);worker.process(row.pk);self.assertEqual(Draft.objects.filter(action='ddk').count(),1)
    def test_photos_with_reason_can_be_submitted_as_one_message(self):
        draft=self.begin();self.method(draft,'p2');self.event('1,5');self.event('8');self.event(str(self.today))
        with patch('maxbot.ddk.download_photo',return_value=self.image()):self.event('ДДК: ТЕСТ основание',photos=[{'type':'image','url':'https://cdn.invalid/photo'}])
        draft.refresh_from_db();self.assertEqual(draft.state,'photos');self.assertEqual(draft.photos.count(),1)
    def test_foreign_binding_cannot_edit_or_confirm_another_draft(self):
        draft=self.fill();other=self.bind(777,888,user=self.leader)
        self.event(f'ddk:save:{draft.pk}',sender=777,chat=888);self.assertFalse(Report.objects.exists())
        draft.refresh_from_db();self.assertEqual(draft.state,'photos')


    def test_old_confirmation_cannot_save_revised_answers(self):
        draft=self.fill();self.event(f'ddk:review:{draft.pk}');draft.refresh_from_db()
        old=f'ddk:save:{draft.pk}:{ddk.signature(draft)}'
        self.event(f'ddk:edit:{draft.pk}');self.method(draft,'p1');self.event('4');self.event('2+2');self.event('10');self.event(str(self.today));self.event('ТЕСТ другой размер')
        self.event(f'ddk:review:{draft.pk}');self.event(old);self.assertFalse(Report.objects.exists())
        self.save(draft);self.job.refresh_from_db();self.assertEqual(self.job.length_m,4)
    def test_old_default_button_on_same_step_after_edit_is_rejected(self):
        draft=self.fill();self.event(f'ddk:edit:{draft.pk}');self.method(draft,'p1');draft.refresh_from_db()
        old=f'ddk:default:{draft.pk}:1:{draft.raw["_ddk_question"]}'
        self.event('4');self.event(f'ddk:edit:{draft.pk}');self.method(draft,'p1');self.event(old)
        draft.refresh_from_db();self.assertEqual(draft.step,1);self.assertEqual(draft.raw['length_m'],'4')
    def test_unknown_commands_never_fall_into_lk_only_report_form(self):
        draft=self.begin();self.event('/help')
        row=Outbox.objects.filter(key__endswith=':reply').latest('created_at')
        self.assertNotIn('ЛК',row.body['text']);self.assertIn('ДДК',row.body['text'])
        draft.refresh_from_db();self.assertEqual(draft.step,0)
    def test_internal_preparer_can_link_and_act_with_same_scope_as_lk(self):
        from projects.models import ObjectAccess,ContractAccess
        user=User.objects.create_user('+70000000004',full_name='ТЕСТ подготовка',role='technician',must_change_password=False)
        AccessGrant.objects.create(user=user,permission='repairs.prepare',granted_by=self.leader)
        ObjectAccess.objects.create(user=user,project_object=self.obj,granted_by=self.leader)
        ContractAccess.objects.create(user=user,contract=self.contract,granted_by=self.leader)
        self.assertTrue(links.supported(user));binding=self.bind(777,888,user=user)
        def event(text):self.event(text,sender=777,chat=888)
        event(f'ddk:select:{self.job.pk}');draft=Draft.objects.get(binding=binding,action='ddk')
        event(f'ddk:answer:{draft.pk}:0:{draft.raw["_ddk_question"]}:p2')
        for text in ('1,5','8',str(self.today),'ТЕСТ после ДДК'):event(text)
        event(f'ddk:review:{draft.pk}')
        message=Outbox.objects.filter(binding=binding,scope__ddk_state='confirm').latest('created_at');self.assertTrue(worker.delivery_allowed(message))
        draft.refresh_from_db();event(f'ddk:save:{draft.pk}:{ddk.signature(draft)}')
        self.assertEqual(Report.objects.get().author,user)
    def test_full_http_webhook_flow_saves_pdf_without_browser_navigation(self):
        counter=0
        def send(text='',callback=False,attachments=None):
            nonlocal counter
            counter+=1
            message={'recipient':{'chat_type':'dialog','chat_id':201}}
            if callback:
                payload={'update_type':'message_callback','message':message,'callback':{'user':{'user_id':101},'timestamp':counter,'callback_id':f'TEST-DDK-{counter}','payload':text}}
            else:
                message.update(timestamp=counter,sender={'user_id':101},body={'mid':f'TEST-DDK-{counter}','text':text,'attachments':attachments or []})
                payload={'update_type':'message_created','message':message}
            response=self.client.post(reverse('max_webhook'),json.dumps(payload),content_type='application/json',HTTP_X_MAX_BOT_API_SECRET=base.CONFIG['ARMADA_MAX_WEBHOOK_SECRET'])
            self.assertEqual(response.status_code,200);worker.tick()
            return payload
        def choose(label):
            row=Outbox.objects.filter(key__endswith=':reply').latest('created_at')
            button=next(b[0] for b in row.body['attachments'][0]['payload']['buttons'] if b[0]['text']==label)
            self.assertEqual(button['type'],'callback');send(button['payload'],True)
        send('/ддк')
        send(f'worknav:jobs:ddk:{self.section.pk}:0')
        row=Outbox.objects.filter(key__endswith=':reply').latest('created_at')
        send(row.body['attachments'][0]['payload']['buttons'][0][0]['payload'],True)
        choose('П2')
        for text in ('1,5','8',self.today.strftime('%d.%m.%y'),'ТЕСТ заключение ДДК'):send(text)
        with patch('maxbot.ddk.download_file',return_value=SimpleUploadedFile('conclusion.pdf',b'%PDF-TEST')):
            send(attachments=[{'type':'file','filename':'conclusion.pdf','payload':{'url':'https://cdn.invalid/doc','token':'REUSE_SECRET_NOT_PERSIST'}}])
        choose('Проверить изменение');choose('Сохранить изменение')
        self.assertEqual(Report.objects.count(),1);self.assertEqual(Attachment.objects.get().name,'conclusion.pdf')
        for row in Outbox.objects.filter(scope__ddk_draft__isnull=False):
            self.assertFalse(any(b[0]['type']=='link' for b in row.body.get('attachments',[{'payload':{'buttons':[]}}])[0]['payload']['buttons']))
        self.assertFalse(Inbox.objects.exclude(payload={}).exists())


class DDKTransportTests(TestCase):
    @override_settings(ARMADA_MAX_FILE_HOSTS=('cdn.invalid',))
    def test_file_fetch_is_bounded_and_has_no_api_token(self):
        addresses=[(None,None,None,None,('8.8.8.8',443))]
        response=MagicMock();response.__enter__.return_value.read.return_value=b'%PDF-TEST'
        opener=MagicMock();opener.open.return_value=response
        with patch('maxbot.transport.socket.getaddrinfo',return_value=addresses),patch('maxbot.transport.build_opener',return_value=opener):
            file=transport.download_file('https://cdn.invalid/document','ddk.pdf')
        self.assertEqual(file.name,'ddk.pdf');request=opener.open.call_args.args[0];self.assertNotIn('Authorization',request.headers)
        self.assertEqual(response.__enter__.return_value.read.call_args.args[0],10*1024*1024+1)
    @override_settings(ARMADA_MAX_FILE_HOSTS=('cdn.invalid',))
    def test_private_unknown_http_and_redirect_sources_are_denied(self):
        for url in ('http://cdn.invalid/a','https://unknown.invalid/a','https://user:pass@cdn.invalid/a','https://cdn.invalid:8080/a','https://[broken'):
            with self.assertRaises(ValidationError):transport.download_file(url,'ddk.pdf')
        with patch('maxbot.transport.socket.getaddrinfo',return_value=[(None,None,None,None,('127.0.0.1',443))]),self.assertRaises(ValidationError):transport.download_file('https://cdn.invalid/a','ddk.pdf')
    def test_normalize_keeps_file_name_but_not_reuse_token(self):
        data={'update_type':'message_created','message':{'timestamp':1,'sender':{'user_id':101},'recipient':{'chat_type':'dialog','chat_id':201},'body':{'mid':'TEST-MID','attachments':[{'type':'file','filename':'ddk.pdf','payload':{'url':'https://cdn.invalid/file','token':'DO-NOT-KEEP'}}]}}}
        result=views.normalize(data)[3];self.assertEqual(result['photos'][0]['name'],'ddk.pdf');self.assertNotIn('DO-NOT-KEEP',json.dumps(result))
