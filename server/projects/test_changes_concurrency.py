import uuid,threading
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from django.test import TransactionTestCase
from django.db import connection,connections,close_old_connections
from django.core.exceptions import ValidationError
from accounts.models import User
from .models import LegalEntity,ProjectObject,Contract,ContractObject,ContractChange,ContractChangeDecision,COMPANY_TAX_ID
from .changes import ChangeForm,submit,decide

@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL row locks')
class CustomerChangeConcurrencyTests(TransactionTestCase):
    def setUp(self):
        entity=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})[0]
        self.users=[User.objects.create_user('+70000000631','test-secret',full_name='ТЕСТ 1',leader_slot=1),User.objects.create_user('+70000000632','test-secret',full_name='ТЕСТ 2',leader_slot=2)]
        self.obj=ProjectObject.objects.create(legal_entity=entity,name='ТЕСТ')
        self.contract=Contract.objects.create(legal_entity=entity,number='ТЕСТ',subject='ТЕСТ',work_kinds=['diagnostics'])
        ContractObject.objects.create(contract=self.contract,project_object=self.obj)
        form=ChangeForm({'token':str(uuid.uuid4()),'expected_version':0,'title':'ТЕСТ','received_on':'2026-07-15','effective_from':'2026-07-16','objects':[str(self.obj.pk)],'scope':'Секция 1','before_text':'300','after_text':'200','basis_kind':'oral','basis_note':'ТЕСТ','impacts':['cost']},user=self.users[0],contract=self.contract)
        assert form.is_valid(),form.errors
        self.data=form.cleaned_data
    def race(self,fn):
        barrier=threading.Barrier(2)
        def run(i):
            close_old_connections()
            try:barrier.wait(10);fn(i);return 'saved'
            except ValidationError:return 'conflict'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(run,[0,1]))
    def test_concurrent_retry_creates_one_revision(self):
        actor=self.users[0]
        self.assertEqual(self.race(lambda i:submit(User.objects.get(pk=actor.pk),self.contract.pk,None,self.data)),['saved','saved'])
        self.assertEqual(ContractChange.objects.count(),1);self.assertEqual(ContractChange.objects.get().revisions.count(),1)
    def test_two_leaders_cannot_make_opposing_decisions(self):
        change=submit(self.users[0],self.contract.pk,None,self.data)
        self.assertCountEqual(self.race(lambda i:decide(self.users[i],change.pk,change.version,['approved','revision'][i],'ТЕСТ решение')),['saved','conflict'])
        self.assertEqual(ContractChangeDecision.objects.count(),1)
