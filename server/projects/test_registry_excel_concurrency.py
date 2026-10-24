import threading,uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from django.test import TransactionTestCase
from django.db import connection,connections,close_old_connections
from django.core.exceptions import ValidationError
from accounts.models import User
from documents.models import Person
from .models import LegalEntity,ProjectObject,COMPANY_TAX_ID,RegistryImportReceipt
from .registry_excel import commit_preview

@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL row locks')
class RegistryConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})[0]
        self.users=[User.objects.create_user('+70000000711','test-secret',full_name='ТЕСТ 1',leader_slot=1),User.objects.create_user('+70000000712','test-secret',full_name='ТЕСТ 2',leader_slot=2)]
    def race(self,fn):
        barrier=threading.Barrier(2)
        def run(i):
            close_old_connections()
            try:barrier.wait(10);fn(i);return 'saved'
            except ValidationError:return 'conflict'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(run,[0,1]))
    def test_repeated_preview_creates_one_person_under_concurrent_retry(self):
        user=self.users[0];payload={'actor':str(user.pk),'session':user.session_version,'kind':'people','token':str(uuid.uuid4()),'rows':[{'row':2,'values':['','ТЕСТ сотрудник','Мастер','0','']}]}
        result=self.race(lambda i:commit_preview(User.objects.get(pk=user.pk),'people',payload))
        self.assertEqual(result,['saved','saved']);self.assertEqual(Person.objects.count(),1);self.assertEqual(RegistryImportReceipt.objects.count(),1)
    def test_two_editors_cannot_overwrite_same_version(self):
        obj=ProjectObject.objects.create(legal_entity=self.company,name='Исходное')
        def edit(i):
            actor=self.users[i];payload={'actor':str(actor.pk),'session':actor.session_version,'kind':'objects','token':str(uuid.uuid4()),'rows':[{'row':2,'values':[str(obj.pk),'ТЕСТ '+str(i),'','','В работе','','','1']}]}
            commit_preview(actor,'objects',payload)
        self.assertCountEqual(self.race(edit),['saved','conflict']);obj.refresh_from_db();self.assertEqual(obj.version,2);self.assertEqual(RegistryImportReceipt.objects.count(),1)
