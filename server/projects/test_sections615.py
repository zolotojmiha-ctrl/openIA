from django.test import TestCase
from django.urls import reverse
from accounts.models import User
from .models import LegalEntity,COMPANY_TAX_ID,Contract,ContractObject,ProjectObject
from repairs.models import Section,Job,Defect

class ContractAreaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.leader=User.objects.create_user('+79999981501','123456',leader_slot=1,full_name='Руководитель',must_change_password=False)
        entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
        cls.contract=Contract.objects.create(legal_entity=entity,number='К-615',work_kinds=['repairs'])
        cls.other=Contract.objects.create(legal_entity=entity,number='К-ДРУГОЙ',work_kinds=['repairs'])
        cls.area=ProjectObject.objects.create(legal_entity=entity,name='Татарская — Омск',status='active')
        ContractObject.objects.create(contract=cls.contract,project_object=cls.area)
    def setUp(self):self.client.force_login(self.leader)
    def url(self,contract=None):return reverse('area_defect_new',args=[(contract or self.contract).pk,self.area.pk])
    def data(self,**kw):return {'pipeline':'ОИ','rnu':'Новосибирское РНУ','kilometre':'123.456','defect_numbers':'1001','method':'grind','materials':'unknown',**kw}
    def test_direct_defect_creation_without_nested_area_preserves_context(self):
        response=self.client.post(self.url(),self.data())
        self.assertEqual(response.status_code,302, str(response.context["form"].errors) if response.context else "")
        self.assertEqual(ProjectObject.objects.count(),1)
        job=Job.objects.get();self.assertEqual(job.contract,self.contract);self.assertEqual(job.section.project_object,self.area)
        self.assertEqual(job.section.pipeline,'ОИ');self.assertEqual(job.defects.get().number,'1001')
        page=self.client.get(reverse('work_place',args=[self.contract.pk,self.area.pk]))
        self.assertContains(page,'123.456');self.assertContains(page,'1001');self.assertContains(page,'Добавить дефект');self.assertNotContains(page,'Добавить участок')
        self.assertEqual(page.context['defect_count'],1)
    def test_invalid_submission_does_not_leave_ledger_or_defect(self):
        response=self.client.post(self.url(),self.data(kilometre='ошибка'))
        self.assertEqual(response.status_code,200);self.assertFalse(Section.objects.exists());self.assertFalse(Job.objects.exists());self.assertFalse(Defect.objects.exists())
    def test_cannot_post_into_unrelated_contract_or_override_context(self):
        self.assertEqual(self.client.post(self.url(self.other),self.data()).status_code,404)
        response=self.client.post(self.url(),self.data(contract=str(self.other.pk)))
        self.assertEqual(response.status_code,302);self.assertEqual(Job.objects.get().contract,self.contract)
    def test_same_km_different_pipelines_are_separate_and_ledger_reused(self):
        for pipeline,number in [('ОИ','1001'),('УБО','1001'),('ОИ','1002')]:
            self.assertEqual(self.client.post(self.url(),self.data(pipeline=pipeline,defect_numbers=number)).status_code,302)
        self.assertEqual(Section.objects.count(),2);self.assertEqual(Defect.objects.count(),3)
    def test_contract_card_has_large_scoped_areas_no_global_objects_navigation(self):
        page=self.client.get(reverse('contract_detail',args=[self.contract.pk]))
        self.assertContains(page,'Участки работ');self.assertContains(page,'Добавить участок работ')
        self.assertContains(page,reverse('work_place',args=[self.contract.pk,self.area.pk]))
        self.assertNotContains(page,'▱ Объекты')

    def test_excel_ledger_setup_stays_in_area_and_contract(self):
        url=reverse('repair_section_new')+'?contract='+str(self.contract.pk)+'&object='+str(self.area.pk)+'&next=import'
        response=self.client.post(url,{'project_object':str(self.area.pk),'rnu':'Новосибирское РНУ','pipeline':'УБО','name':self.area.name})
        self.assertEqual(response.status_code,302)
        self.assertIn(reverse('repair_import'),response.url)
        self.assertIn(str(self.contract.pk),response.url)
        section=Section.objects.get();self.assertEqual(section.project_object,self.area)
        self.assertFalse(Job.objects.exists())
    def test_cannot_attach_existing_area_to_second_contract(self):
        from .forms import ContractForm
        form=ContractForm({'number':'Новый','work_kinds':['repairs'],'status':'active','linked_objects':[str(self.area.pk)]},user=self.leader)
        self.assertFalse(form.is_valid());self.assertIn('linked_objects',form.errors)
    def test_no_permission_or_archived_area_cannot_create_defect(self):
        user=User.objects.create_user('+79999981502','123456',full_name='Работник',must_change_password=False)
        self.client.force_login(user);self.assertEqual(self.client.get(self.url()).status_code,403)
        self.client.force_login(self.leader);self.area.status='archived';self.area.save()
        self.assertEqual(self.client.post(self.url(),self.data()).status_code,404)
