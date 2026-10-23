import threading,secrets
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import connection,connections,close_old_connections,transaction
from django.test import TransactionTestCase
from accounts.models import User
from .models import Contract,LegalEntity,ProjectObject,ContractObject,COMPANY_TAX_ID
from .closure import transition
from repairs.models import Job,Section,Defect,JobDefect

@skipUnless(connection.vendor=='postgresql','Проверка блокировок требует PostgreSQL')
class ClosureConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.leader=User.objects.create_user('+79990009101',secrets.token_urlsafe(20),leader_slot=1,full_name='ТЕСТ',must_change_password=False)
        company,_=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        self.contract=Contract.objects.create(legal_entity=company,number='ТЕСТ',subject='ТЕСТ',status='active')
        obj=ProjectObject.objects.create(legal_entity=company,name='ТЕСТ');ContractObject.objects.create(contract=self.contract,project_object=obj)
        self.section=Section.objects.create(project_object=obj,name='ТЕСТ',rnu='ТЕСТ',pipeline='ТЕСТ')
        transition(self.leader,self.contract.pk,1,'request','ТЕСТ')
    def test_writer_does_not_deadlock_when_closure_holds_parent(self):
        job=Job.objects.create(contract=self.contract,section=self.section,method='grind',created_by=self.leader,state='confirmed')
        locked=threading.Event();release=threading.Event()
        def holder():
            close_old_connections()
            try:
                with transaction.atomic():
                    Contract.objects.select_for_update().get(pk=self.contract.pk)
                    locked.set()
                    if not release.wait(10):raise RuntimeError('Timeout')
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(holder)
            try:
                self.assertTrue(locked.wait(10))
                with transaction.atomic():
                    Job.objects.select_for_update(no_key=True).get(pk=job.pk)
                    with self.assertRaises(PermissionDenied):Job.objects.filter(pk=job.pk).update(notes='Не сохранять')
            finally:release.set()
            future.result(timeout=15)
        job.refresh_from_db();self.assertEqual(job.notes,'')
    def test_closure_rechecks_work_committed_before_parent_lock(self):
        written=threading.Event();release=threading.Event()
        def writer():
            close_old_connections()
            try:
                with transaction.atomic():
                    job=Job.objects.create(contract=self.contract,section=self.section,method='grind',created_by=self.leader)
                    d=Defect.objects.create(contract=self.contract,section=self.section,kilometre=1,number='1');JobDefect.objects.create(job=job,defect=d)
                    written.set()
                    if not release.wait(10):raise RuntimeError('Timeout')
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(writer)
            self.assertTrue(written.wait(10));release.set();future.result(timeout=15)
        with self.assertRaises(ValidationError):transition(self.leader,self.contract.pk,2,'archive','ТЕСТ')
        self.contract.refresh_from_db();self.assertEqual(self.contract.status,'active')
