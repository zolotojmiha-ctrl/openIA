import secrets,threading
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from django.db import connection,connections,close_old_connections
from django.test import TransactionTestCase
from django.core.exceptions import ValidationError
from django.utils import timezone
from accounts.models import User
from projects.models import LegalEntity,ProjectObject,COMPANY_TAX_ID
from .models import Job,Section,Defect,JobDefect,Report
from .review_services import confirm_many

@skipUnless(connection.vendor=='postgresql','Проверка блокировок требует PostgreSQL')
class ReviewConcurrencyTests(TransactionTestCase):
    def test_two_leaders_confirm_same_selection_once(self):
        leaders=[User.objects.create_user(f'+7000000000{i}',secrets.token_urlsafe(24),leader_slot=i,full_name='ТЕСТ',must_change_password=False) for i in (1,2)]
        company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})[0]
        obj=ProjectObject.objects.create(legal_entity=company,name='ТЕСТ')
        section=Section.objects.create(project_object=obj,rnu='ТЕСТ',pipeline='ТЕСТ',name='ТЕСТ')
        jobs=[]
        for n in (1,2):
            j=Job.objects.create(section=section,method='grind',state='reported',created_by=leaders[0]);jobs.append(j)
            d=Defect.objects.create(section=section,kilometre=n,number=str(n));JobDefect.objects.create(job=j,defect=d)
            Report.objects.create(job=j,author=leaders[0],kind='complete',outcome='complete',method='grind',performed_date=timezone.localdate())
        snapshot={str(j.pk):j.version for j in jobs};barrier=threading.Barrier(2)
        def run(i):
            close_old_connections()
            try:
                barrier.wait(timeout=10);confirm_many(leaders[i],snapshot);return 'saved'
            except ValidationError:return 'conflict'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[0,1]))
        self.assertCountEqual(results,['saved','conflict'])
        self.assertEqual(Job.objects.filter(state='confirmed').count(),2)
        self.assertEqual(len(set(Report.objects.values_list('confirmed_by_id',flat=True))),1)
