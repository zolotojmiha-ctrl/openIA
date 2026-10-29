import hashlib
import io
import json
import smtplib
import uuid
import zipfile
from datetime import timedelta
from unittest.mock import patch
from cryptography.fernet import Fernet
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.utils import timezone
from .models import SiteRequest, SiteRequestAttempt, Mailbox
from .crypto import seal
from . import site_forms, site_delivery
from accounts.models import User


@override_settings(ARMADA_MAIL_KEY=Fernet.generate_key().decode())
class SiteFormsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.leader=User.objects.create_user('+70000000201','123456',full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        cls.second=User.objects.create_user('+70000000202','123456',full_name='ТЕСТ Второй',leader_slot=2,must_change_password=False)
        cls.worker=User.objects.create_user('+70000000203','123456',full_name='ТЕСТ Сотрудник',must_change_password=False)

    def payload(self,kind='candidate',client=None,**changes):
        client=client or self.client
        token=client.get(reverse('site_form_token'),{'kind':kind}).json()
        common={'phone':'+7 (900) 000-00-01','consent':'on','token':token['token']}
        if kind=='candidate':common.update(name='ТЕСТ Кандидат',email='candidate@example.invalid',city='ТЕСТ Город',profession='Монтажник',experience='Опыт 5 лет',travel='Готов(а)',about='')
        elif kind in ('couplings','centralizers'):common.update(name='ТЕСТ Заказчик',email='customer@example.invalid',company='ТЕСТ',type='П2' if kind=='couplings' else 'Наружный звеньевой центратор',dn='720',quantity='2',region='ТЕСТ',details='ТЕСТ')
        else:common['equipment']='Экскаватор'
        return common|changes

    def send(self,kind='candidate',**changes):
        return self.client.post(reverse('site_form_submit',args=[kind]),self.payload(kind,**changes))

    def box(self,owner=None):
        return Mailbox.objects.create(address=site_forms.RECIPIENT,owner=owner,state='active',credential=seal('synthetic-password'),created_by=self.leader)

    def row(self,**changes):
        self.send(**changes)
        return SiteRequest.objects.latest('created_at')

    def test_all_four_forms_are_received_without_login(self):
        for kind in ('candidate','rental','couplings','centralizers'):
            response=self.send(kind);self.assertEqual(response.status_code,200,response.content)
            self.assertTrue(response.json()['ok']);self.assertEqual(len(response.json()['reference']),12)
        self.assertEqual(SiteRequest.objects.count(),4)
        self.assertEqual(set(SiteRequest.objects.values_list('consent_version',flat=True)),{site_forms.CONSENT_VERSION})

    def test_csrf_is_required_and_token_endpoint_sets_cookie(self):
        client=Client(enforce_csrf_checks=True)
        data=self.payload(client=client)
        self.assertIn('csrftoken',client.cookies)
        url=reverse('site_form_submit',args=['candidate'])
        self.assertEqual(client.post(url,data).status_code,403)
        token=client.get(reverse('site_form_token'),{'kind':'candidate'}).json()
        self.assertEqual(client.post(url,data,HTTP_X_CSRFTOKEN=token['csrf']).status_code,200)
        self.assertEqual(SiteRequest.objects.count(),1)

    def test_cross_site_origin_is_rejected(self):
        client=Client(enforce_csrf_checks=True);data=self.payload(client=client)
        token=client.get(reverse('site_form_token'),{'kind':'candidate'}).json()
        self.assertEqual(client.post(reverse('site_form_submit',args=['candidate']),data,HTTP_X_CSRFTOKEN=token['csrf'],HTTP_ORIGIN='https://foreign.example').status_code,403)

    def test_consent_is_required_and_not_preselected(self):
        response=self.send(consent='');self.assertEqual(response.status_code,400);self.assertFalse(SiteRequest.objects.exists())
        self.assertIn('consent',response.json()['errors'])

    def test_required_fields_phone_and_choice_errors_do_not_save(self):
        for changes in [{'name':''},{'phone':'123'},{'email':'not-an-address'},{'experience':''},{'travel':'forged'}]:
            with self.subTest(changes=changes):self.assertEqual(self.send(**changes).status_code,400)
        self.assertFalse(SiteRequest.objects.exists())

    def test_signed_token_binds_form_and_cannot_be_forged(self):
        data=self.payload('rental')
        self.assertEqual(self.client.post(reverse('site_form_submit',args=['candidate']),data).status_code,400)
        self.assertEqual(self.send(token='forged').status_code,400)
        self.assertFalse(SiteRequest.objects.exists())
        self.assertEqual(self.client.get(reverse('site_form_token'),{'kind':'unknown'}).status_code,404)

    def test_duplicate_retry_is_one_receipt_and_changed_data_is_rejected(self):
        data=self.payload();url=reverse('site_form_submit',args=['candidate'])
        first=self.client.post(url,data)
        self.assertEqual(self.client.post(url,data).json(),first.json())
        self.assertEqual(self.client.post(url,data|{'name':'Другой'}).status_code,409)
        self.assertEqual(SiteRequest.objects.count(),1)

    def test_pdf_is_kept_private_and_preserves_bytes(self):
        content=b'%PDF-1.4\nsynthetic resume'
        self.assertEqual(self.send(resume=SimpleUploadedFile('resume.pdf',content)).status_code,200)
        row=SiteRequest.objects.get();self.assertEqual(bytes(row.resume_data),content)
        url=reverse('site_request_resume',args=[row.pk])
        self.assertEqual(self.client.get(url).status_code,302)
        self.client.force_login(self.worker);self.assertEqual(self.client.get(url).status_code,403)
        self.client.force_login(self.leader);response=self.client.get(url)
        self.assertEqual(b''.join(response.streaming_content),content)
        self.assertIn('attachment',response['Content-Disposition']);self.assertEqual(response['Content-Type'],'application/octet-stream')
        self.assertIn('no-store',response['Cache-Control'])

    def test_normal_docx_is_accepted_and_fake_docx_is_rejected(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:z.writestr('[Content_Types].xml','<Types/>');z.writestr('word/document.xml','<document/>')
        self.assertEqual(self.send(resume=SimpleUploadedFile('resume.docx',stream.getvalue())).status_code,200)
        self.assertEqual(self.send(resume=SimpleUploadedFile('bad.docx',b'not a word file')).status_code,400)
        self.assertEqual(SiteRequest.objects.count(),1)

    def test_empty_wrong_type_and_oversized_resume_are_not_saved(self):
        for name,content in [('resume.pdf',b''),('resume.exe',b'MZbinary'),('fake.pdf',b'MZbinary'),('huge.pdf',b'%PDF-'+b'x'*(5*1024*1024))]:
            with self.subTest(name=name):self.assertIn(self.send(resume=SimpleUploadedFile(name,content)).status_code,[400,413])
        self.assertFalse(SiteRequest.objects.exists())

    def test_non_candidate_cannot_attach_files_and_unknown_files_are_rejected(self):
        self.assertEqual(self.send('rental',resume=SimpleUploadedFile('resume.pdf',b'%PDF-1.4')).status_code,400)
        self.assertEqual(self.send(other=SimpleUploadedFile('resume.pdf',b'%PDF-1.4')).status_code,400)
        self.assertFalse(SiteRequest.objects.exists())

    def test_multiple_files_and_declared_oversize_are_rejected_before_save(self):
        data=self.payload(resume=[SimpleUploadedFile('a.pdf',b'%PDF-1.4'),SimpleUploadedFile('b.pdf',b'%PDF-1.4')])
        self.assertEqual(self.client.post(reverse('site_form_submit',args=['candidate']),data).status_code,413)
        self.assertFalse(SiteRequest.objects.exists())

    def test_honeypot_and_ip_rate_limit(self):
        self.assertEqual(self.send('rental',website='spam').status_code,400)
        for _ in range(10):self.assertEqual(self.send('rental').status_code,200)
        self.assertEqual(self.send('rental').status_code,429)
        self.assertEqual(SiteRequest.objects.count(),10)
        self.assertNotIn('127.0.0.1',SiteRequestAttempt.objects.get().key)
        SiteRequestAttempt.objects.update(window_start=timezone.now()-timedelta(hours=2))
        self.assertEqual(self.send('rental').status_code,200)

    def test_forwarded_address_only_used_with_explicit_proxy_trust(self):
        from django.test import RequestFactory
        request=RequestFactory().get('/',REMOTE_ADDR='10.0.0.2',HTTP_X_FORWARDED_FOR='198.51.100.7')
        with override_settings(SECURE_PROXY_SSL_HEADER=None):direct=site_forms.client_key(request)
        with override_settings(SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https')):forwarded=site_forms.client_key(request)
        self.assertNotEqual(direct,forwarded)

    def test_both_leaders_see_requests_and_read_state_is_individual(self):
        row=self.row();self.client.force_login(self.leader)
        self.assertContains(self.client.get(reverse('mail')),'Новых: 1')
        self.assertEqual(self.client.get(reverse('site_request',args=[row.pk])).status_code,200)
        self.assertEqual(SiteRequest.objects.exclude(read_by=self.leader).count(),0)
        self.assertEqual(SiteRequest.objects.exclude(read_by=self.second).count(),1)
        self.client.force_login(self.second);self.assertContains(self.client.get(reverse('site_requests')),'Анкета кандидата')

    def test_only_designated_info_owner_gets_access(self):
        row=self.row();self.box(owner=self.worker);self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse('site_request',args=[row.pk])).status_code,200)
        self.assertEqual(self.client.post(reverse('site_request_delete',args=[row.pk])).status_code,403)

    def test_ordinary_user_cannot_list_read_or_download(self):
        row=self.row();self.client.force_login(self.worker)
        for route,args in [('site_requests',[]),('site_request',[row.pk]),('site_request_resume',[row.pk])]:
            self.assertEqual(self.client.get(reverse(route,args=args)).status_code,403)

    def test_values_are_escaped_in_the_cabinet(self):
        row=self.row(name='<script>alert(1)</script>');self.client.force_login(self.leader)
        response=self.client.get(reverse('site_request',args=[row.pk]));self.assertNotContains(response,'<script>alert(1)</script>')
        self.assertContains(response,'&lt;script&gt;alert(1)&lt;/script&gt;')

    def test_missing_info_delays_mail_but_does_not_lose_request(self):
        row=self.row()
        with patch('postoffice.transport.deliver') as send:self.assertTrue(site_delivery.process_one());send.assert_not_called()
        row.refresh_from_db();self.assertEqual(row.delivery_state,'queued');self.assertGreater(row.next_attempt_at,timezone.now())
        self.box();SiteRequest.objects.update(next_attempt_at=timezone.now()-timedelta(seconds=1))
        with patch('postoffice.transport.deliver',return_value={}) as send:self.assertTrue(site_delivery.process_one())
        row.refresh_from_db();self.assertEqual(row.delivery_state,'sent');self.assertEqual(send.call_args.args[2],[site_forms.RECIPIENT])

    def test_smtp_message_has_fixed_recipient_reply_to_and_original_resume(self):
        box=self.box();content=b'%PDF-1.4\nresume';row=self.row(resume=SimpleUploadedFile('resume.pdf',content),to='attacker@example.invalid')
        msg=site_delivery.message_for(row,box)
        self.assertEqual(msg['To'],site_forms.RECIPIENT);self.assertEqual(msg['From'],site_forms.RECIPIENT)
        self.assertEqual(msg['Reply-To'],'candidate@example.invalid');self.assertNotIn('attacker',msg.as_string())
        self.assertEqual(list(msg.iter_attachments())[0].get_payload(decode=True),content)

    def test_auth_rejection_is_failed_and_disconnect_is_uncertain(self):
        self.box();row=self.row()
        with patch('postoffice.transport.deliver',side_effect=ValidationError('private-password')):self.assertTrue(site_delivery.process_one())
        row.refresh_from_db();self.assertEqual(row.delivery_state,'failed');self.assertNotIn('private-password',row.delivery_error)
        SiteRequest.objects.update(delivery_state='queued')
        with patch('postoffice.transport.deliver',side_effect=OSError('disconnect')):self.assertTrue(site_delivery.process_one())
        row.refresh_from_db();self.assertEqual(row.delivery_state,'uncertain')
        with patch('postoffice.transport.deliver') as send:self.assertFalse(site_delivery.process_one());send.assert_not_called()

    def test_uncertain_resend_requires_leader_and_explicit_check(self):
        row=self.row();SiteRequest.objects.update(delivery_state='uncertain');url=reverse('site_request_retry',args=[row.pk])
        self.client.force_login(self.leader);self.assertEqual(self.client.post(url).status_code,400)
        self.assertEqual(self.client.post(url,{'checked':'1'}).status_code,302)
        row.refresh_from_db();self.assertEqual(row.delivery_state,'queued')

    def test_interrupted_send_is_not_blindly_retried(self):
        row=self.row();SiteRequest.objects.update(delivery_state='sending',updated_at=timezone.now()-timedelta(minutes=11))
        site_delivery.recover_interrupted();row.refresh_from_db();self.assertEqual(row.delivery_state,'uncertain')

    def test_cleanup_and_leader_delete_remove_private_resume(self):
        row=self.row();row.read_by.add(self.leader)
        SiteRequest.objects.update(created_at=timezone.now()-timedelta(days=181))
        site_delivery.cleanup_expired();self.assertFalse(SiteRequest.objects.exists())
        row=self.row();self.client.force_login(self.leader)
        self.assertEqual(self.client.post(reverse('site_request_delete',args=[row.pk])).status_code,302)
        self.assertFalse(SiteRequest.objects.exists())

    def test_public_page_has_live_forms_and_no_editorial_banners(self):
        from pathlib import Path
        from django.conf import settings
        source=(settings.BASE_DIR/'index.html').read_text()
        for text in ['Версия для согласования','Проект политики','Предпросмотр формы','Приём анкет пока не открыт','Формулировка для согласования']:self.assertNotIn(text,source)
        self.assertNotIn('версия для согласования',source.lower())
        self.assertEqual(source.count('data-site-form='),4)
        self.assertEqual(source.count('name="consent" required'),4)
        self.assertIn('name="travel"',source)

    def test_real_tls_delivery_to_fixed_info_mailbox(self):
        from .test_transport import TransportTLSTests, SMTP
        from email import policy
        from email.parser import BytesParser
        self.box();self.row(resume=SimpleUploadedFile('resume.pdf',b'%PDF-1.4\nTLS resume'))
        with TransportTLSTests().server(SMTP) as server,override_settings(ARMADA_SMTP_HOST='localhost',ARMADA_SMTP_PORT=server.server_address[1],ARMADA_SMTP_STARTTLS=False):
            self.assertTrue(site_delivery.process_one())
        message=BytesParser(policy=policy.default).parsebytes(server.message)
        self.assertEqual(message['To'],site_forms.RECIPIENT)
        self.assertEqual(message['Reply-To'],'candidate@example.invalid')
        self.assertEqual(list(message.iter_attachments())[0].get_payload(decode=True),b'%PDF-1.4\nTLS resume')
        self.assertEqual(SiteRequest.objects.get().delivery_state,'sent')
