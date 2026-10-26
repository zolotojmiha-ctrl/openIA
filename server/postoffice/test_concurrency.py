import threading,uuid
from unittest import skipUnless
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from cryptography.fernet import Fernet
from django.test import TransactionTestCase,override_settings
from django.db import connection,connections,close_old_connections
from accounts.models import User
from projects.models import LegalEntity,COMPANY_TAX_ID
from .services import request_address,configure,save_letter,process_one
from .models import Letter

@skipUnless(connection.vendor=='postgresql','Очередь проверяется на PostgreSQL')
@override_settings(ARMADA_MAIL_KEY=Fernet.generate_key().decode())
class MailConcurrencyTests(TransactionTestCase):
    def test_two_workers_do_not_send_the_same_letter_twice(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        leader=User.objects.create_user('+70000000991','123456',leader_slot=1,full_name='ТЕСТ',must_change_password=False)
        box=request_address(leader,'test@armada-sibir.ru',leader);box=configure(leader,box.pk,1,'synthetic',leader,None,True)
        row=save_letter(leader,box.pk,uuid.uuid4(),0,{'to':'test@example.org','subject':'ТЕСТ','body':'ТЕСТ'},[],queue=True)
        barrier=threading.Barrier(2)
        def worker(_):
            close_old_connections()
            try:barrier.wait(timeout=10);return process_one()
            finally:connections.close_all()
        with patch('postoffice.transport.deliver',return_value={}) as send,patch('postoffice.transport.save_sent'):
            with ThreadPoolExecutor(max_workers=2) as pool:self.assertCountEqual(list(pool.map(worker,[0,1])),[True,False])
            self.assertEqual(send.call_count,1)
        row.refresh_from_db();self.assertEqual(row.state,'sent')
