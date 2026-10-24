import os,secrets,subprocess,tempfile
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from accounts.models import User,AccessGrant
from projects.models import LegalEntity,ProjectObject,Contract,ContractObject,ObjectAccess,ContractAccess,COMPANY_TAX_ID
from repairs.models import Section,Job,Defect,JobDefect
from documents.models import Person

class FoundationBrowserTests(StaticLiveServerTestCase):
    def test_technician_creates_links_and_assigns_contract_in_real_ui(self):
        password=secrets.token_urlsafe(24)
        leader=User.objects.create_user('+70000000001',password,leader_slot=1,full_name='ТЕСТ Руководитель',must_change_password=False)
        tech=User.objects.create_user('+70000000002',password,role='technician',full_name='ТЕСТ Техник',must_change_password=False)
        company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ Компания'})[0]
        obj=ProjectObject.objects.create(legal_entity=company,name='Омск–Татарск · ТЕСТ')
        contract=Contract.objects.create(legal_entity=company,number='ТЗС-1726-2025 · ТЕСТ',subject='Устранение дефектов',work_kinds=['repairs'])
        ContractObject.objects.create(contract=contract,project_object=obj)
        ObjectAccess.objects.create(user=tech,project_object=obj,granted_by=leader)
        ContractAccess.objects.create(user=tech,contract=contract,granted_by=leader,show_number=True,show_subject=True)
        for key in ('users.manage','projects.manage','projects.access','personnel.manage','repairs.prepare','document.upload'):
            AccessGrant.objects.create(user=tech,permission=key,granted_by=leader)
        section=Section.objects.create(project_object=obj,rnu='Омское РНУ',pipeline='Омск–Татарск',name='Участок 1')
        legacy=Job.objects.create(section=section,method='p2',length_m=1,created_by=leader)
        d=Defect.objects.create(section=section,kilometre='153.546',number='0001',pipe_section='130001');JobDefect.objects.create(job=legacy,defect=d)
        obj_b=ProjectObject.objects.create(legal_entity=company,name='Другая площадка · ТЕСТ')
        ObjectAccess.objects.create(user=tech,project_object=obj_b,granted_by=leader)
        ContractObject.objects.create(contract=contract,project_object=obj_b)
        untyped=Contract.objects.create(legal_entity=company,number='Демо без вида работ · ТЕСТ',subject='ТЕСТ',work_kinds=[])
        for place in [obj,obj_b]:ContractObject.objects.create(contract=untyped,project_object=place)
        ContractAccess.objects.create(user=tech,contract=untyped,granted_by=leader,show_number=True,show_subject=True)
        section_b=Section.objects.create(project_object=obj_b,rnu='ТЕСТ',pipeline='ТЕСТ',name='Другая площадка')
        legacy_b=Job.objects.create(section=section_b,method='grind',created_by=leader)
        defect_b=Defect.objects.create(section=section_b,kilometre='160.100',number='0002',pipe_section='140001')
        JobDefect.objects.create(job=legacy_b,defect=defect_b)
        env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password,ARMADA_CONTRACT_ID=str(contract.pk),ARMADA_SECTION_ID=str(section.pk),ARMADA_OBJECT_ID=str(obj.pk),ARMADA_LEGACY_ID=str(legacy.pk),ARMADA_UNTYPED_CONTRACT_ID=str(untyped.pk))
        with tempfile.TemporaryDirectory() as files,override_settings(PRIVATE_DOCUMENT_ROOT=files):
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/foundation-browser.cjs')],env=env,capture_output=True,text=True,timeout=180)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        p=Person.objects.get(full_name='ТЕСТ Зарипов');self.assertIsNotNone(p.user_id)
        self.assertTrue(ObjectAccess.objects.filter(user=p.user,project_object=obj).exists());self.assertTrue(ContractAccess.objects.filter(user=p.user,contract=contract).exists())
        created=Job.objects.get(defects__number='0008');self.assertEqual(created.contract,contract);self.assertEqual(created.assigned_to,p.user)
        legacy.refresh_from_db();self.assertEqual(legacy.contract,contract)
        legacy_b.refresh_from_db();self.assertEqual(legacy_b.contract,contract)
        untyped.refresh_from_db();self.assertEqual(untyped.work_kinds,['repairs'])
        self.assertEqual(Contract.objects.get(number='УЗА-2026 · ТЕСТ').work_kinds,['valves','diagnostics'])
