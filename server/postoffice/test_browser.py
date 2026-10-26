import os,subprocess
from contextlib import contextmanager
from unittest.mock import patch,MagicMock
from cryptography.fernet import Fernet
from django.test import override_settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.conf import settings
from django.utils import timezone
from accounts.models import User
from documents.models import Person
from projects.models import LegalEntity,COMPANY_TAX_ID,ProjectObject,Contract,ContractObject,ObjectAccess,ContractAccess
from projects.crews import create_crew,assign_crew_place
from projects.daily import DailyWork
from .services import request_address,configure
from .models import Letter

@override_settings(ARMADA_MAIL_KEY=Fernet.generate_key().decode())
class MailDailyBrowserTests(StaticLiveServerTestCase):
    def test_internal_mail_and_daily_execution(self):
        company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})[0]
        leader=User.objects.create_user('+70000000801','123456',leader_slot=1,full_name='ТЕСТ Руководитель',must_change_password=False)
        worker=User.objects.create_user('+70000000802','123456',role='foreman',full_name='ТЕСТ Мастер',must_change_password=False)
        person=Person.objects.create(legal_entity=company,full_name='ТЕСТ Мастер',user=worker,created_by=leader)
        obj=ProjectObject.objects.create(legal_entity=company,name='ТЕСТ Секция 214 км')
        contract=Contract.objects.create(legal_entity=company,number='ТЕСТ-Д',subject='Диагностика',work_kinds=['diagnostics','insulation'])
        ContractObject.objects.create(contract=contract,project_object=obj);ObjectAccess.objects.create(user=worker,project_object=obj,granted_by=leader);ContractAccess.objects.create(user=worker,contract=contract,granted_by=leader,show_number=True)
        crew=create_crew(leader,'ТЕСТ Бригада',person,[],timezone.localdate());assignment=assign_crew_place(leader,crew.pk,crew.version,contract.pk,obj.pk,timezone.localdate(),None)
        box=request_address(leader,'worker@armada-sibir.ru',worker);configure(leader,box.pk,box.version,'synthetic-mail-password',worker,None,True)
        flags={'seen':False}
        def uid(command,*args):
            if command=='STORE':flags['seen']=True
            return ('OK',[b'' if flags['seen'] else b'1'])
        @contextmanager
        def connection(_):
            c=MagicMock();c.uid.side_effect=uid;yield c
        def listing(client,folder,page=1,search='',unread_only=False):
            rows=[{'uid':'1','subject':'ТЕСТ Письмо','sender':'sender@example.org','sender_short':'Отправитель','short_date':'12:01','date':'Сегодня','unread':not flags['seen']}]
            rows += [{'uid':str(i),'subject':'ТЕСТ Входящее письмо № '+str(i),'sender':'other@example.org','sender_short':'Другой отправитель','short_date':'11:45','date':'Сегодня','unread':False} for i in range(2,26)]
            if unread_only:rows=[r for r in rows if r['unread']]
            return rows,len(rows)
        env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_BOX=str(box.pk),ARMADA_DAILY=f'/cabinet/work/contracts/{contract.pk}/places/{obj.pk}/daily/diagnostics/',ARMADA_CREW_ASSIGNMENT=str(assignment.pk),ARMADA_TODAY=timezone.localdate().isoformat())
        with patch('postoffice.transport.connect',connection),patch('postoffice.transport.folders',return_value=[{'name':'INBOX','label':'Входящие'}]),patch('postoffice.transport.listing',side_effect=listing),patch('postoffice.transport.select'),patch('postoffice.transport.message',return_value={'subject':'ТЕСТ Письмо','sender':'sender@example.org','to':box.address,'date':'Сегодня','body':'<script>window.mailBad=true</script> Текст письма','attachments':[],'reply_to':'sender@example.org'}):
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/mail-daily-browser.cjs')],env=env,capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertTrue(flags['seen']);box.refresh_from_db();self.assertEqual(box.unread,0)
        self.assertEqual(Letter.objects.get().state,'queued');self.assertEqual(DailyWork.objects.get().state,'confirmed')
