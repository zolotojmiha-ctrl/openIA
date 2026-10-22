import secrets,threading
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from django.db import connection,connections,close_old_connections
from django.test import TransactionTestCase
from django.core.exceptions import ValidationError
from accounts.models import User
from projects.models import LegalEntity,ProjectObject,Contract,ContractObject,COMPANY_TAX_ID
from repairs.models import Job,Section,Defect,JobDefect
from .workspace import allocate_legacy

@skipUnless(connection.vendor=='postgresql','Проверка блокировок требует PostgreSQL')
class FoundationConcurrencyTests(TransactionTestCase):
    def test_two_contracts_cannot_claim_same_legacy_work(self):
        leaders=[User.objects.create_user(f'+7000000000{i}',secrets.token_urlsafe(24),leader_slot=i,full_name='ТЕСТ',must_change_password=False) for i in (1,2)]
        company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})[0]
        obj=ProjectObject.objects.create(legal_entity=company,name='Общий участок')
        contracts=[Contract.objects.create(legal_entity=company,number=str(i),subject='ТЕСТ',work_kinds=['repairs']) for i in (1,2)]
        for c in contracts:ContractObject.objects.create(contract=c,project_object=obj)
        section=Section.objects.create(project_object=obj,rnu='ТЕСТ',pipeline='ТЕСТ',name='ТЕСТ')
        job=Job.objects.create(section=section,method='p2',length_m=1,created_by=leaders[0])
        d=Defect.objects.create(section=section,kilometre=1,number='001');JobDefect.objects.create(job=job,defect=d)
        barrier=threading.Barrier(2)
        def run(i):
            close_old_connections()
            try:
                barrier.wait(timeout=10);allocate_legacy(leaders[i],contracts[i],[job.pk],{str(job.pk):1});return 'saved'
            except ValidationError:return 'conflict'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[0,1]))
        self.assertCountEqual(results,['saved','conflict']);job.refresh_from_db();d.refresh_from_db();self.assertEqual(job.contract_id,d.contract_id);self.assertEqual(job.version,2)
