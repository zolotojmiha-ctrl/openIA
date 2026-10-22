import os,secrets,subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.utils import timezone
from accounts.models import User
from documents.models import Person
from .models import LegalEntity,ProjectObject,Contract,ContractObject,COMPANY_TAX_ID,Brigade
from repairs.models import Section,Job,Defect,JobDefect,Report

class CrewReviewBrowserTests(StaticLiveServerTestCase):
    def test_crews_and_review_on_desktop_and_phone(self):
        password=secrets.token_urlsafe(24)
        leader=User.objects.create_user('+70000000001',password,leader_slot=1,full_name='ТЕСТ Руководитель',must_change_password=False)
        company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})[0]
        person=Person.objects.create(legal_entity=company,full_name='ТЕСТ Мастер',position='Мастер',created_by=leader,user=leader)
        obj=ProjectObject.objects.create(legal_entity=company,name='ТЕСТ Омск–Татарск')
        contract=Contract.objects.create(legal_entity=company,number='ТЕСТ-1726',subject='Устранение дефектов',work_kinds=['repairs']);ContractObject.objects.create(contract=contract,project_object=obj)
        section=Section.objects.create(project_object=obj,rnu='ТЕСТ Омское РНУ',pipeline='ТЕСТ Омск–Иркутск',name='Омск–Татарск')
        jobs=[]
        for n in range(1,4):
            j=Job.objects.create(contract=contract,section=section,method='grind',state='reported',created_by=leader);jobs.append(j)
            d=Defect.objects.create(contract=contract,section=section,kilometre=f'153.{n:03}',number=f'{n:04}',pipe_section='130001');JobDefect.objects.create(job=j,defect=d)
            Report.objects.create(job=j,author=leader,kind='complete',outcome='complete',method='grind',performed_date=timezone.localdate(),latitude=55,longitude=73,note='ТЕСТ результат')
        env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password,ARMADA_PERSON_ID=str(person.pk),ARMADA_CONTRACT_ID=str(contract.pk),ARMADA_OBJECT_ID=str(obj.pk),ARMADA_JOB_IDS=','.join(str(j.pk) for j in jobs))
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/crews-review-browser.cjs')],env=env,capture_output=True,text=True,timeout=180)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertTrue(Brigade.objects.filter(name='ТЕСТ Бригада № 1').exists())
        self.assertEqual(Job.objects.filter(state='confirmed').count(),2)
        jobs[2].refresh_from_db();self.assertEqual(jobs[2].review_note,'Уточните координаты точки')
