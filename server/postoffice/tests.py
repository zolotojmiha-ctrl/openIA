import uuid,smtplib
from unittest.mock import patch,MagicMock
from contextlib import contextmanager
from cryptography.fernet import Fernet
from django.test import TestCase,override_settings
from django.urls import reverse
from django.core.exceptions import ValidationError,PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from accounts.models import User,AccessGrant,AuditEvent
from documents.models import Person
from projects.models import LegalEntity,COMPANY_TAX_ID
from .models import Mailbox,Letter,Attachment
from .access import boxes
from .crypto import seal,reveal
from . import services,transport

@override_settings(ARMADA_MAIL_KEY=Fernet.generate_key().decode())
class MailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.leader=User.objects.create_user('+70000000101','123456',full_name='Руководитель',leader_slot=1,must_change_password=False)
        cls.second=User.objects.create_user('+70000000102','123456',full_name='Второй руководитель',leader_slot=2,must_change_password=False)
        cls.worker=User.objects.create_user('+70000000103','123456',full_name='Сотрудник',must_change_password=False)
        cls.other=User.objects.create_user('+70000000104','123456',full_name='Другой',must_change_password=False)
        cls.company=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
    def box(self,owner=None):
        box=services.request_address(self.leader,'worker@armada-sibir.ru',owner or self.worker)
        return services.configure(self.leader,box.pk,1,'private-provider-password',owner or self.worker,None,True)
    def data(self,**kw):return dict(to='recipient@example.org',cc='',bcc='',subject='Тема',body='Письмо',**kw)
    def letter(self,box=None,queue=True):return services.save_letter(self.worker,(box or self.box()).pk,uuid.uuid4(),0,self.data(),[],queue=queue)
    def test_encryption_and_no_key_failure(self):
        token=seal('secret');self.assertNotIn('secret',token);self.assertEqual(reveal(token),'secret')
        with override_settings(ARMADA_MAIL_KEY=''):
            with self.assertRaises(ValidationError):seal('secret')
        with override_settings(ARMADA_MAIL_KEY=Fernet.generate_key().decode()):
            with self.assertRaises(ValidationError):reveal(token)
    def test_both_leaders_and_only_owner_can_read(self):
        box=self.box();self.assertTrue(boxes(self.second).filter(pk=box.pk).exists());self.assertFalse(boxes(self.other).exists())
        self.client.force_login(self.other)
        for route in ['mail_inbox','mail_compose','mail_outbox','mailbox_configure']:
            self.assertIn(self.client.get(reverse(route,args=[box.pk])).status_code,[403,404])
        self.assertNotContains(self.client.get(reverse('mail')),'worker@')
    def test_nonleader_cannot_manage_even_with_user_permission(self):
        AccessGrant.objects.create(user=self.worker,permission='users.manage',granted_by=self.leader)
        self.client.force_login(self.worker);self.assertEqual(self.client.get(reverse('mailbox_new')).status_code,403)
        with self.assertRaises(PermissionDenied):services.request_address(self.worker,'bad@armada-sibir.ru')
    def test_request_is_honestly_pending_without_external_creation(self):
        self.client.force_login(self.leader)
        r=self.client.post(reverse('mailbox_new'),{'address':'new@armada-sibir.ru','owner':self.worker.pk})
        self.assertEqual(r.status_code,302);box=Mailbox.objects.get();self.assertEqual(box.state,'requested');self.assertFalse(box.credential)
        self.assertContains(self.client.get(r.url),'Автоматическое создание ящиков через API Beget пока не подключено')
    def test_person_account_link_grants_exact_employee(self):
        p=Person.objects.create(legal_entity=self.company,full_name='Без аккаунта',created_by=self.leader)
        box=services.request_address(self.leader,'person@armada-sibir.ru',person=p)
        self.assertFalse(boxes(self.worker).exists());p.user=self.worker;p.save();self.assertTrue(boxes(self.worker).exists())
        p.archived=True;p.save();self.assertFalse(boxes(self.worker).exists())
    def test_unrelated_person_and_owner_rejected(self):
        p=Person.objects.create(legal_entity=self.company,full_name='Другой',user=self.other,created_by=self.leader)
        with self.assertRaises(ValidationError):services.request_address(self.leader,'bad@armada-sibir.ru',self.worker,p)
    def test_configure_never_returns_password_to_browser(self):
        box=self.box();self.client.force_login(self.leader)
        r=self.client.get(reverse('mailbox_configure',args=[box.pk]));self.assertNotContains(r,'private-provider-password');self.assertNotContains(r,box.credential)
        with self.assertRaises(ValidationError):services.configure(self.leader,box.pk,1,'changed',self.worker,None,True)
    def test_disabled_owner_and_disabled_mailbox_prevent_send(self):
        row=self.letter();Mailbox.objects.filter(pk=row.mailbox_id).update(state='disabled')
        with patch('postoffice.transport.deliver') as send:self.assertTrue(services.process_one());send.assert_not_called()
        row.refresh_from_db();self.assertEqual(row.state,'failed')
    def test_draft_queue_and_replay_does_not_duplicate(self):
        row=self.letter(queue=False);self.assertEqual(row.state,'draft')
        services.save_letter(self.worker,row.mailbox_id,row.pk,row.version,self.data(),[],queue=True)
        with self.assertRaises(ValidationError):services.save_letter(self.worker,row.mailbox_id,row.pk,row.version,self.data(),[],queue=True)
        self.assertEqual(Letter.objects.count(),1)
    def test_header_injection_and_wrong_owner_rejected(self):
        box=self.box()
        with self.assertRaises(ValidationError):services.save_letter(self.worker,box.pk,uuid.uuid4(),0,{**self.data(),'to':'a@example.org\r\nBcc: thief@example.org'},[],queue=True)
        row=self.letter(box,False)
        with self.assertRaises(PermissionDenied):services.save_letter(self.leader,box.pk,row.pk,row.version,self.data(),[],queue=True)
        self.assertEqual(Letter.objects.count(),1)
    def test_attachment_and_bcc_are_in_envelope_not_header(self):
        box=self.box();row=services.save_letter(self.worker,box.pk,uuid.uuid4(),0,{**self.data(),'bcc':'hidden@example.org'},[SimpleUploadedFile('file.txt',b'attachment')],queue=True)
        msg=services.message_for(row);self.assertIsNone(msg['Bcc']);self.assertEqual(len(list(msg.iter_attachments())),1)
        with patch('postoffice.transport.deliver',return_value={}) as send,patch('postoffice.transport.save_sent'):
            services.process_one();self.assertIn('hidden@example.org',send.call_args.args[2])
        row.refresh_from_db();self.assertEqual(row.state,'sent')
    def test_ambiguous_send_is_not_retried(self):
        row=self.letter()
        with patch('postoffice.transport.deliver',side_effect=smtplib.SMTPServerDisconnected):services.process_one()
        row.refresh_from_db();self.assertEqual(row.state,'uncertain')
        with patch('postoffice.transport.deliver') as send:self.assertFalse(services.process_one());send.assert_not_called()
    def test_acceptance_then_copy_failure_stays_sent(self):
        row=self.letter()
        with patch('postoffice.transport.deliver',return_value={}),patch('postoffice.transport.save_sent',side_effect=ValidationError('copy failed')):services.process_one()
        row.refresh_from_db();self.assertEqual(row.state,'sent');self.assertIn('копию',row.error)
    def test_smtp_rejection_is_failed(self):
        row=self.letter()
        with patch('postoffice.transport.deliver',side_effect=smtplib.SMTPAuthenticationError(535,b'private')):services.process_one()
        row.refresh_from_db();self.assertEqual(row.state,'failed');self.assertNotIn('private',row.error)
    def test_html_mail_is_plain_text_without_active_markup(self):
        raw=b'From: sender@example.org\r\nSubject: hello\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Hello<script>bad()</script><img src="https://tracker.invalid/x"></p>'
        c=MagicMock();c.list.return_value=('OK',[b'(\\HasNoChildren) "/" "INBOX"']);c.select.return_value=('OK',[b'1'])
        c.uid.side_effect=[('OK',[b'1 (RFC822.SIZE 200)']),('OK',[(b'header',raw),b')'])]
        result=transport.message(c,'INBOX',1);self.assertIn('Hello',result['body']);self.assertNotIn('bad()',result['body']);self.assertNotIn('tracker',result['body'])
    def test_employee_creation_can_request_mail_without_auto_provisioning(self):
        self.client.force_login(self.leader)
        r=self.client.post(reverse('employee_new'),{'full_name':'Новый','phone':'+70000000105','role':'foreman','is_active':'on','permissions':[],'request_mail':'on','mail_address':'new@armada-sibir.ru'})
        self.assertEqual(r.status_code,200);box=Mailbox.objects.get();self.assertEqual(box.state,'requested');self.assertEqual(box.owner.phone,'+70000000105')
    def test_person_creation_can_request_mail_later(self):
        self.client.force_login(self.leader)
        r=self.client.post(reverse('person_new'),{'full_name':'Новый','position':'Мастер','request_mail':'on','mail_address':'new@armada-sibir.ru'})
        self.assertEqual(r.status_code,302);self.assertIsNotNone(Mailbox.objects.get().person_id)
    def test_mailbox_excel_preview_and_apply_are_private_and_idempotent(self):
        from .excel import HEADERS
        from repairs.excel import workbook
        self.client.force_login(self.leader)
        raw=SimpleUploadedFile('boxes.xlsx',workbook([['','new@armada-sibir.ru',str(self.worker.pk),'','requested','']],headers=HEADERS))
        response=self.client.post(reverse('mail_excel'),{'file':raw});self.assertFalse(Mailbox.objects.exists());self.assertTrue(response.context['preview'],response.context['form'].errors)
        token=response.context['preview'];self.client.post(reverse('mail_excel'),{'apply':'1','preview':token});self.client.post(reverse('mail_excel'),{'apply':'1','preview':token})
        self.assertEqual(Mailbox.objects.count(),1)
        self.client.force_login(self.worker);self.assertEqual(self.client.get(reverse('mail_excel')).status_code,403)
    def test_mailbox_transfer_revokes_old_owner_and_pending_send(self):
        box=self.box();row=self.letter(box)
        services.configure(self.leader,box.pk,box.version,'',self.other,None,True)
        self.assertFalse(boxes(self.worker).exists());self.assertTrue(boxes(self.other).exists())
        with patch('postoffice.transport.deliver') as send:services.process_one();send.assert_not_called()
        row.refresh_from_db();self.assertEqual(row.state,'failed')
    def test_mail_local_attachment_cannot_be_read_by_other_employee(self):
        box=self.box();row=services.save_letter(self.worker,box.pk,uuid.uuid4(),0,self.data(),[SimpleUploadedFile('note.txt',b'private')],queue=False)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('mail_local_attachment',args=[box.pk,row.pk,row.attachments.get().pk])).status_code,404)
        self.client.force_login(self.worker);self.assertEqual(self.client.get(reverse('mail_local_attachment',args=[box.pk,row.pk,row.attachments.get().pk])).content,b'private')
    def test_unread_mail_updates_badge_and_notification_target(self):
        box=self.box();Mailbox.objects.filter(pk=box.pk).update(unread=2,unread_digest='new-uids')
        self.client.force_login(self.worker);data=self.client.get(reverse('notifications')).json()
        self.assertEqual(data['mail'],2);self.assertTrue(any(k.startswith('m:') for k in data['keys']));self.assertEqual(data['url'],reverse('mail'))
        self.client.force_login(self.other);data=self.client.get(reverse('notifications')).json();self.assertEqual(data['mail'],0);self.assertFalse(any(k.startswith('m:') for k in data['keys']))
    def test_disabling_mailbox_hides_local_drafts_from_employee(self):
        box=self.box();row=self.letter(box,False)
        services.configure(self.leader,box.pk,box.version,'',self.worker,None,False)
        self.client.force_login(self.worker);self.assertEqual(self.client.get(reverse('mail_local_letter',args=[box.pk,row.pk])).status_code,404)

    def test_attachment_server_failure_returns_to_inbox(self):
        box=self.box();self.client.force_login(self.worker)
        with patch('postoffice.transport.connect',side_effect=ValidationError('Почтовый сервер недоступен.')):
            response=self.client.get(reverse('mail_attachment',args=[box.pk,1,0]))
        self.assertEqual(response.status_code,302)
        self.assertIn(reverse('mail_inbox',args=[box.pk]),response.url)

    def test_cyrillic_letter_within_upload_limit(self):
        from django.test.client import encode_multipart,BOUNDARY,MULTIPART_CONTENT
        box=self.box();self.client.force_login(self.worker)
        data={**self.data(),'body':'Я'*24000,'letter_id':str(uuid.uuid4()),'version':0,'action':'draft'}
        payload=encode_multipart(BOUNDARY,data)
        self.assertLess(len(payload),128*1024)
        response=self.client.generic('POST',reverse('mail_compose',args=[box.pk]),data=payload,content_type=MULTIPART_CONTENT)
        self.assertEqual(response.status_code,302)
        self.assertEqual(Letter.objects.get().body,'Я'*24000)
