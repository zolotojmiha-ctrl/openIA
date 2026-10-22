import secrets,threading,uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from django.db import connection,connections,close_old_connections
from django.test import TransactionTestCase
from django.core.exceptions import ValidationError
from django.utils import timezone
from accounts.models import User
from .models import LegalEntity,ProjectObject,Contract,ContractObject,SubcontractDecision,SubcontractResult,COMPANY_TAX_ID
from . import subcontracts as s

@skipUnless(connection.vendor=='postgresql','Проверка блокировок требует PostgreSQL')
class SubcontractConcurrencyTests(TransactionTestCase):
    def test_two_leaders_cannot_review_same_result_twice(self):
        leaders=[User.objects.create_user(f'+7000000000{i}',secrets.token_urlsafe(24),leader_slot=i,full_name='ТЕСТ',must_change_password=False) for i in (1,2)]
        entity=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})[0];obj=ProjectObject.objects.create(legal_entity=entity,name='ТЕСТ')
        contract=Contract.objects.create(legal_entity=entity,number='ТЕСТ',subject='ТЕСТ',work_kinds=['valves']);ContractObject.objects.create(contract=contract,project_object=obj)
        company=s.save_company(leaders[0],None,None,dict(name='ТЕСТ',tax_id='0000000001',contact='',notes='',archived=False));order=s.save_order(leaders[0],None,None,dict(company=company,contract=contract,project_object=obj,kind='valves',title='ТЕСТ',scope='',unit='',planned_quantity=None,responsible=None,archived=False))
        s.submit_result(leaders[0],order.pk,dict(token=uuid.uuid4(),expected_version=1,outcome='complete',date=timezone.localdate(),quantity=None,note='ТЕСТ',files=[]));order.refresh_from_db();barrier=threading.Barrier(2)
        def run(i):
            close_old_connections()
            try:barrier.wait(timeout=10);s.decide(leaders[i],order.pk,order.version,'confirmed');return 'saved'
            except ValidationError:return 'conflict'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[0,1]))
        self.assertCountEqual(results,['saved','conflict']);self.assertEqual(SubcontractDecision.objects.count(),1);self.assertEqual(SubcontractResult.objects.count(),1)
