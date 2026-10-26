import threading,uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from django.test import TransactionTestCase
from django.db import connection,close_old_connections,connections
from django.core.exceptions import ValidationError,PermissionDenied
from django.utils import timezone
from accounts.models import User
from .models import LegalEntity,COMPANY_TAX_ID,Contract,ProjectObject,ContractObject,DailyWorkRevision,DailyWorkDecision
from .daily import submit,decide

@skipUnless(connection.vendor=='postgresql','Проверка блокировок требует PostgreSQL')
class DailyConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.users=[User.objects.create_user('+7000000090'+str(i),'123456',leader_slot=i,full_name='ТЕСТ',must_change_password=False) for i in (1,2)]
        company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})[0]
        self.obj=ProjectObject.objects.create(legal_entity=company,name='ТЕСТ')
        self.contract=Contract.objects.create(legal_entity=company,number='ТЕСТ',work_kinds=['diagnostics'])
        ContractObject.objects.create(contract=self.contract,project_object=self.obj)
        self.data={'record':str(uuid.uuid4()),'version':'0','date':str(timezone.localdate()),'stage':'grind_long','place':'ТЕСТ','length':'1','width':'300'}
        self.row=submit(self.users[0],self.contract.pk,self.obj.pk,'diagnostics',self.data)
    def race(self,fn):
        barrier=threading.Barrier(2)
        def run(i):
            close_old_connections()
            try:barrier.wait(timeout=10);fn(i);return 'saved'
            except (ValidationError,PermissionDenied):return 'conflict'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertCountEqual(list(pool.map(run,[0,1])),['saved','conflict'])
    def test_two_reviews_do_not_overwrite_each_other(self):
        self.race(lambda i:decide(self.users[i],self.row.pk,1,'confirmed'));self.assertEqual(DailyWorkDecision.objects.count(),1)
    def test_simultaneous_revisions_preserve_both_old_and_selected_new(self):
        self.race(lambda i:submit(self.users[i],self.contract.pk,self.obj.pk,'diagnostics',{**self.data,'version':'1','width':str(200+i)}))
        self.assertEqual(DailyWorkRevision.objects.count(),2)
