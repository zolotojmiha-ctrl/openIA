import os,secrets,subprocess
from unittest.mock import patch
from cryptography.fernet import Fernet
from django.test import override_settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.conf import settings
from accounts.models import User
from projects.models import LegalEntity,COMPANY_TAX_ID
from . import services,test_shared_trash as shared
from .models import TrashEntry


@override_settings(ARMADA_MAIL_KEY=Fernet.generate_key().decode())
class SharedMailBrowserTests(StaticLiveServerTestCase):
    def test_share_then_select_trash_restore_on_phone(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        password=secrets.token_urlsafe(20)
        leader=User.objects.create_user('+70000000901',password,leader_slot=1,full_name='ТЕСТ Руководитель',must_change_password=False)
        first=User.objects.create_user('+70000000902',password,role='mechanic',full_name='ТЕСТ Механик',must_change_password=False)
        second=User.objects.create_user('+70000000903',password,role='pto',full_name='ТЕСТ ПТО',must_change_password=False)
        box=services.request_address(leader,'shared@armada-sibir.ru',first)
        box=services.configure(leader,box.pk,box.version,'SYNTHETIC-PASSWORD',first,None,True)
        server=shared.MemoryIMAP()
        with patch('postoffice.transport.connect',server.connection):
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/shared-mail-browser.cjs')],env=os.environ|{
                'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,'ARMADA_BOX':str(box.pk),
                'ARMADA_MEMBER_ONE':str(first.pk),'ARMADA_MEMBER_TWO':str(second.pk)},capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(box.members.count(),2);self.assertEqual(len(server.data['INBOX']),3)
        self.assertEqual(TrashEntry.objects.count(),0)
