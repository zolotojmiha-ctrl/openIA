import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from unittest.mock import patch
from cryptography.fernet import Fernet
from django.core import signing
from django.db import connection, connections
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from accounts.models import User
from .crypto import seal
from .models import SiteRequest, SiteRequestAttempt, Mailbox
from .site_forms import SALT, client_key
from . import site_delivery


@skipUnless(connection.vendor=='postgresql','Требуется PostgreSQL для проверки одновременных запросов')
@override_settings(ARMADA_MAIL_KEY=Fernet.generate_key().decode(),SECURE_PROXY_SSL_HEADER=None)
class SiteFormsConcurrency(TransactionTestCase):
    def payload(self,token=None):
        return {'phone':'+79000000001','equipment':'ТЕСТ','consent':'on',
                'token':token or signing.dumps({'id':str(uuid.uuid4()),'kind':'rental'},salt=SALT)}

    def parallel(self,values,callback):
        barrier=threading.Barrier(len(values))
        def run(value):
            try:barrier.wait(timeout=10);return callback(value)
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=len(values)) as pool:return list(pool.map(run,values))

    def test_retry_from_two_networks_has_one_receipt(self):
        data=self.payload()
        results=self.parallel(['198.51.100.1','198.51.100.2'],lambda ip:Client().post(reverse('site_form_submit',args=['rental']),data,REMOTE_ADDR=ip).status_code)
        self.assertEqual(results,[200,200]);self.assertEqual(SiteRequest.objects.count(),1)

    def test_rate_limit_is_serialized(self):
        from django.test import RequestFactory
        key=client_key(RequestFactory().get('/',REMOTE_ADDR='198.51.100.1'))
        SiteRequestAttempt.objects.create(key=key,count=8)
        results=self.parallel([self.payload() for _ in range(4)],lambda data:Client().post(reverse('site_form_submit',args=['rental']),data,REMOTE_ADDR='198.51.100.1').status_code)
        self.assertEqual(sorted(results),[200,200,429,429]);self.assertEqual(SiteRequest.objects.count(),2)

    def test_two_workers_send_one_notification(self):
        leader=User.objects.create_user('+70000000501','123456',leader_slot=1,full_name='ТЕСТ',must_change_password=False)
        Mailbox.objects.create(address='info@armada-sibir.ru',state='active',credential=seal('synthetic-secret'),created_by=leader)
        self.assertEqual(Client().post(reverse('site_form_submit',args=['rental']),self.payload()).status_code,200)
        def deliver(*args):time.sleep(.1);return {}
        with patch('postoffice.transport.deliver',side_effect=deliver) as send:
            results=self.parallel([1,2],lambda _:site_delivery.process_one())
        self.assertEqual(sorted(results),[False,True]);self.assertEqual(send.call_count,1)
        self.assertEqual(SiteRequest.objects.get().delivery_state,'sent')
