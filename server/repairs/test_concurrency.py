import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from django.core.exceptions import ValidationError
from django.db import connection,close_old_connections,connections
from django.test import TransactionTestCase
from django.utils import timezone
from accounts.models import User
from projects.models import LegalEntity,ProjectObject,ObjectAccess,COMPANY_TAX_ID
from .models import Section,Job,Coupling,Report,CouplingUse
from .services import report_work,confirm_work

@skipUnless(connection.vendor=='postgresql','Проверка блокировок требует PostgreSQL')
class RepairConcurrencyTests(TransactionTestCase):
    def setUp(self):
        password=secrets.token_urlsafe(24)
        self.leaders=[User.objects.create_user(f'+7000000000{i}',password,full_name=f'ТЕСТ Руководитель {i}',leader_slot=i,must_change_password=False) for i in (1,2)]
        self.workers=[User.objects.create_user(f'+7000000000{i}',password,full_name=f'ТЕСТ Мастер {i}',role='foreman',must_change_password=False) for i in (3,4)]
        company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ Компания'})[0]
        obj=ProjectObject.objects.create(legal_entity=company,name='Учебный объект')
        for user in self.workers:ObjectAccess.objects.create(user=user,project_object=obj,granted_by=self.leaders[0])
        section=Section.objects.create(project_object=obj,rnu='ТЕСТ РНУ',pipeline='ТЕСТ труба',name='ТЕСТ участок')
        self.jobs=[Job.objects.create(section=section,created_by=self.leaders[0],assigned_to=user,method='p1',length_m=1) for user in self.workers]
        self.c=Coupling.objects.create(project_object=obj,method='p1',length_mm=1000,serial='ТЕСТ-ЕДИНСТВЕННАЯ')
    def parallel(self,callbacks):
        barrier=threading.Barrier(2)
        def run(callback):
            close_old_connections()
            try:
                barrier.wait(timeout=10);callback();return 'saved'
            except ValidationError:return 'conflict'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(run,callbacks))
    def data(self,token=None):return {'token':token or uuid.uuid4(),'expected_version':1,'performed_date':timezone.localdate(),'outcome':'complete','method':'p1','length_m':1,'composition':'1','couplings':[self.c],'note':'Учебный результат'}
    def test_two_crews_cannot_install_one_physical_coupling(self):
        callbacks=[lambda i=i:report_work(self.workers[i],self.jobs[i].pk,'result',self.data()) for i in (0,1)]
        self.assertCountEqual(self.parallel(callbacks),['saved','conflict']);self.assertEqual(Report.objects.count(),1);self.assertEqual(CouplingUse.objects.count(),1)
    def test_parallel_retries_create_one_report(self):
        token=uuid.uuid4();callbacks=[lambda:report_work(self.workers[0],self.jobs[0].pk,'result',self.data(token)) for _ in range(2)]
        self.assertEqual(self.parallel(callbacks),['saved','saved']);self.assertEqual(Report.objects.count(),1)
    def test_two_leaders_cannot_confirm_stale_result_twice(self):
        report_work(self.workers[0],self.jobs[0].pk,'result',self.data())
        callbacks=[lambda i=i:confirm_work(self.leaders[i],self.jobs[0].pk,2) for i in (0,1)]
        self.assertCountEqual(self.parallel(callbacks),['saved','conflict']);self.jobs[0].refresh_from_db();self.assertEqual(self.jobs[0].state,'confirmed')
