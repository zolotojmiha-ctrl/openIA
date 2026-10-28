from django.test import TestCase
from django.urls import reverse
from accounts.models import User
from projects.models import Contract,ProjectObject,ContractObject,LegalEntity,COMPANY_TAX_ID,CounterpartyAgreement
from documents.models import Person
from repairs.models import Section,Job,Defect,JobDefect
from repairs.access import visible_jobs

class WorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.leader=User.objects.create_user('+79999986161','123456',leader_slot=1,must_change_password=False)
        cls.master=User.objects.create_user('+79999986162','123456',role='foreman',full_name='Зарипов',must_change_password=False)
        cls.other=User.objects.create_user('+79999986163','123456',role='foreman',must_change_password=False)
        cls.entity=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ Компания'})[0]
        cls.contract=Contract.objects.create(legal_entity=cls.entity,number='Тест',rnu='Новосибирское РНУ',subject='Дефекты',work_kinds=['repairs'],status='active')
        cls.area=ProjectObject.objects.create(legal_entity=cls.entity,name='Татарская–Омск',status='active')
        ContractObject.objects.create(contract=cls.contract,project_object=cls.area)
        cls.section=Section.objects.create(project_object=cls.area,rnu=cls.contract.rnu,pipeline='ОИ',name=cls.area.name)
        cls.job=Job.objects.create(contract=cls.contract,section=cls.section,assigned_to=cls.master,method='grind',created_by=cls.leader)
        cls.defect=Defect.objects.create(contract=cls.contract,section=cls.section,kilometre='153',number='1748')
        JobDefect.objects.create(job=cls.job,defect=cls.defect)
    def setUp(self):self.client.force_login(self.leader)
    def test_context_inherits_and_master_does_not_need_broad_acl(self):
        url=reverse('area_defect_new',args=[self.contract.pk,self.area.pk])
        response=self.client.get(url)
        self.assertContains(response,'Зарипов')
        self.assertTrue(response.context['header'].fields['rnu'].disabled)
        self.assertTrue(response.context['header'].fields['pipeline'].disabled)
        response=self.client.post(url,{'kilometre':'154','defect_numbers':'1749','pipe_section':'123','method':'grind','assigned_to':str(self.master.pk),'materials':'ready','open_assignment_access':'on','expected_version':0})
        self.assertEqual(response.status_code,302,str(response.context['form'].errors) if response.status_code==200 else '')
        job=Job.objects.get(defects__number='1749')
        self.assertEqual(job.section.rnu,'Новосибирское РНУ')
        self.assertEqual(job.section_id,self.section.pk)
        self.assertTrue(visible_jobs(self.master).filter(pk=job.pk).exists())
        self.assertFalse(visible_jobs(self.other).filter(pk=job.pk).exists())
        self.client.force_login(self.master)
        self.assertEqual(self.client.get(reverse('repair_detail',args=[job.pk])).status_code,200)
        self.assertEqual(self.client.get(reverse('contract_detail',args=[self.contract.pk])).status_code,200)
        self.assertFalse(self.master.contract_access.get(contract=self.contract).show_counterparty)
    def test_assignment_requires_explicit_scope_and_rejects_forged_context(self):
        url=reverse('area_defect_new',args=[self.contract.pk,self.area.pk])
        payload={'kilometre':'154','defect_numbers':'1751','method':'grind','assigned_to':str(self.other.pk),'materials':'ready','rnu':'Подмена','pipeline':'Подмена'}
        response=self.client.post(url,payload)
        self.assertIn('assigned_to',response.context['form'].errors)
        self.assertFalse(Defect.objects.filter(number='1751').exists())
        self.assertFalse(self.other.object_assignments.exists())
        payload['open_assignment_access']='on'
        self.assertEqual(self.client.post(url,payload).status_code,302)
        defect=Defect.objects.get(number='1751')
        self.assertEqual(defect.section.rnu,self.contract.rnu)
        self.assertEqual(defect.section.pipeline,'ОИ')
    def test_same_register_and_origin_on_both_entrances(self):
        url=reverse('work_place',args=[self.contract.pk,self.area.pk])
        for route in [url,reverse('repairs')+'?contract='+str(self.contract.pk)+'&object='+str(self.area.pk)]:
            response=self.client.get(route)
            self.assertContains(response,'repair-register')
            self.assertContains(response,'Редактировать дефект № 1748')
            self.assertContains(response,'Внести результат')
            self.assertNotContains(response,'Изменить данные')
        response=self.client.get(reverse('repair_detail',args=[self.job.pk]),{'return_to':url+'?q=1748&page=2'})
        self.assertEqual(response.context['back'],url+'?q=1748&page=2')
        response=self.client.get(reverse('repair_detail',args=[self.job.pk]),{'return_to':'https://example.com'})
        self.assertEqual(response.context['back'],url)
    def test_area_menu(self):
        response=self.client.get(reverse('contract_detail',args=[self.contract.pk]))
        self.assertContains(response,'Действия с участком')
        self.assertContains(response,reverse('object_edit',args=[self.area.pk]))
        self.assertContains(response,reverse('area_delete',args=[self.area.pk]))
    def test_delete_empty_contract_preserves_area_and_blocks_history(self):
        contract=Contract.objects.create(legal_entity=self.entity,number='Ошибочный',subject='Тест')
        ContractObject.objects.create(contract=contract,project_object=self.area)
        response=self.client.post(reverse('contract_delete',args=[contract.pk]),{'expected_version':contract.version,'confirm':'on'})
        self.assertEqual(response.status_code,302)
        self.assertFalse(Contract.objects.filter(pk=contract.pk).exists())
        self.assertTrue(ProjectObject.objects.filter(pk=self.area.pk).exists())
        response=self.client.post(reverse('contract_delete',args=[self.contract.pk]),{'expected_version':self.contract.version,'confirm':'on'})
        self.assertContains(response,'Удаление отменено')
        self.assertTrue(Job.objects.filter(pk=self.job.pk).exists())
    def test_delete_archived_contract_unlinks_but_preserves_files(self):
        from documents.models import Document,DocumentContract
        from .archive import lifecycle_write
        contract=Contract.objects.create(legal_entity=self.entity,number='Архивный пустой',subject='Тест')
        doc=Document.objects.create(legal_entity=self.entity,title='Сохранить файл',section='general',created_by=self.leader)
        DocumentContract.objects.create(document=doc,contract=contract)
        ContractObject.objects.create(contract=contract,project_object=self.area)
        with lifecycle_write():
            contract.status='archived';contract.save(update_fields=['status'])
        response=self.client.post(reverse('contract_delete',args=[contract.pk]),{'expected_version':contract.version,'confirm':'on'})
        self.assertEqual(response.status_code,302)
        self.assertTrue(Document.objects.filter(pk=doc.pk).exists())
        self.assertFalse(Contract.objects.filter(pk=contract.pk).exists())
        self.assertTrue(ProjectObject.objects.filter(pk=self.area.pk).exists())
    def test_agreement_delete_stale_and_nonleader(self):
        row=CounterpartyAgreement.objects.create(number='Аренда',kind='housing',counterparty='Тест',subject='Жильё',created_by=self.leader)
        url=reverse('agreement_delete',args=[row.pk])
        self.client.post(url,{'expected_version':0,'confirm':'on'})
        self.assertTrue(CounterpartyAgreement.objects.filter(pk=row.pk).exists())
        self.client.force_login(self.master)
        self.assertEqual(self.client.post(url,{'expected_version':row.version,'confirm':'on'}).status_code,403)
        self.client.force_login(self.leader)
        self.assertEqual(self.client.post(url,{'expected_version':row.version,'confirm':'on'}).status_code,302)
        self.assertFalse(CounterpartyAgreement.objects.filter(pk=row.pk).exists())

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.conf import settings
import subprocess,os,json

class WorkflowBrowser(StaticLiveServerTestCase):
    def test_contract_area_defect_navigation(self):
        WorkflowTests.setUpTestData.__func__(type(self))
        env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_FIXTURE':json.dumps({k:str(v.pk) for k,v in {'contract':self.contract,'area':self.area,'master':self.master,'job':self.job}.items()})}
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/workflow6161-browser.cjs')],env=env,capture_output=True,text=True,timeout=100)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
