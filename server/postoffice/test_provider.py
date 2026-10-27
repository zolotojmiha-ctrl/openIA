from unittest.mock import patch,MagicMock
from contextlib import contextmanager
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import ValidationError
from accounts.models import User
from .models import Mailbox,BegetSettings,MailboxProvision
from .crypto import seal,reveal
from .services import request_address
from .provider import process_one,api

class ProvisionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.leader=User.objects.create_user('+79999981651','123456',leader_slot=1,must_change_password=False)
        cls.customer=User.objects.create_user('+79999981652','123456',role='customer',must_change_password=False)
    def configured(self):return BegetSettings.objects.create(pk=1,login='synthetic',credential=seal('provider-test-password'),enabled=True)
    def test_disabled_provider_keeps_manual_flow_and_never_calls_api(self):
        with patch('postoffice.provider.api') as call:
            box=request_address(self.leader,'manual@armada-sibir.ru')
            self.assertFalse(MailboxProvision.objects.exists());self.assertFalse(process_one());call.assert_not_called()
        self.assertEqual(box.state,'requested')
    def test_opt_in_creates_exactly_once_and_connects_encrypted_mailbox(self):
        self.configured();box=request_address(self.leader,'new@armada-sibir.ru')
        row=MailboxProvision.objects.get();self.assertNotIn('provider-test-password',row.credential)
        with patch('postoffice.provider.api',side_effect=[[],{}]) as call:
            self.assertTrue(process_one());self.assertFalse(process_one());self.assertEqual(call.call_count,2)
            self.assertEqual(call.call_args.args[1],'createMailbox')
            self.assertEqual(call.call_args.args[2]['mailbox'],'new')
        box.refresh_from_db();self.assertEqual(box.state,'active');self.assertEqual(reveal(box.credential),reveal(row.credential))
    def test_existing_mailbox_is_not_reset_or_recreated(self):
        self.configured();box=request_address(self.leader,'existing@armada-sibir.ru')
        with patch('postoffice.provider.api',return_value=[{'mailbox':'existing'}]) as call:self.assertTrue(process_one());self.assertEqual(call.call_count,1)
        box.refresh_from_db();self.assertEqual(box.state,'requested');self.assertEqual(MailboxProvision.objects.get().state,'failed')
    def test_ambiguous_creation_is_not_retried(self):
        self.configured();request_address(self.leader,'uncertain@armada-sibir.ru')
        with patch('postoffice.provider.api',side_effect=[[],ValidationError('Нет ответа')]) as call:
            self.assertTrue(process_one());self.assertFalse(process_one());self.assertEqual(call.call_count,2)
        self.assertEqual(MailboxProvision.objects.get().state,'uncertain')
    def test_api_secrets_are_in_post_body_never_url_and_raw_errors_not_exposed(self):
        config=self.configured();response=MagicMock();response.read.return_value=b'{"status":"success","answer":{"status":"success","result":[]}}'
        opener=MagicMock();opener.open.return_value.__enter__.return_value=response
        with patch('postoffice.provider.build_opener',return_value=opener):self.assertEqual(api(config,'getMailboxList',{'domain':'armada-sibir.ru'}),[])
        request=opener.open.call_args.args[0];self.assertNotIn('password',request.full_url);self.assertEqual(request.get_method(),'POST');self.assertIn(b'provider-test-password',request.data)
        response.read.return_value=b'provider-test-password raw debug'
        with patch('postoffice.provider.build_opener',return_value=opener):
            with self.assertRaises(ValidationError) as cm:api(config,'getMailboxList',{})
        self.assertNotIn('provider-test-password',str(cm.exception))
    def test_only_leader_configures_provider(self):
        self.client.force_login(self.customer);self.assertEqual(self.client.get(reverse('beget_setup')).status_code,403)
