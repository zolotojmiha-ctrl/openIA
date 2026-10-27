import uuid,io
from django.test import TestCase
from django.urls import reverse
from accounts.models import User,AccessGrant
from .models import Contract,ProjectObject,LegalEntity,COMPANY_TAX_ID,ContractAccess
from .access import visible_contracts

class ContractFirstTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.leader=User.objects.create_user('+79999980301','123456',leader_slot=1,full_name='Тест Руководитель',must_change_password=False)
        cls.tech=User.objects.create_user('+79999980302','123456',role='technician',full_name='Тест Техник',must_change_password=False)
        cls.other=User.objects.create_user('+79999980303','123456',role='technician',full_name='Другой',must_change_password=False)
        for u in (cls.tech,cls.other):AccessGrant.objects.create(user=u,permission='projects.manage',granted_by=cls.leader)
    def data(self,**kw):return {'number':'ТЕСТ-КОНТРАКТ-ПЕРВЫЙ','date':'2025-09-15','subject':'Устранение дефектов','counterparty':'Заказчик','deadline':'2028-12-30','amount':'61000000','work_kinds':['repairs'],'status':'active',**kw}
    def test_leader_contract_before_any_object_then_object_from_contract(self):
        self.client.force_login(self.leader)
        self.assertFalse(ProjectObject.objects.exists())
        response=self.client.post(reverse('contract_new'),self.data());self.assertEqual(response.status_code,302)
        c=Contract.objects.get();self.assertFalse(c.objects_linked.exists());self.assertEqual(str(c.amount),'61000000.00')
        self.assertContains(self.client.get(response.url),'Добавить участок работ')
        create=reverse('object_new')+'?contract='+str(c.pk)
        self.assertEqual(self.client.get(create).context['form'].initial['related_contract'],c)
        response=self.client.post(create,{'name':'Объект после контракта','kind':'Нефтепровод','status':'active','related_contract':str(c.pk)})
        self.assertRedirects(response,reverse('contract_detail',args=[c.pk]));c.refresh_from_db();self.assertEqual(c.objects_linked.count(),1);self.assertEqual(c.version,2)
    def test_delegated_creator_keeps_draft_access_but_other_editor_does_not(self):
        self.client.force_login(self.tech)
        data=self.data();data.pop('amount')
        response=self.client.post(reverse('contract_new'),data);self.assertEqual(response.status_code,302)
        c=Contract.objects.get();self.assertTrue(visible_contracts(self.tech).filter(pk=c.pk).exists());self.assertFalse(visible_contracts(self.other).filter(pk=c.pk).exists())
        self.assertEqual(self.client.get(response.url).status_code,200)
        data['expected_version']=1
        self.assertEqual(self.client.post(reverse('contract_edit',args=[c.pk]),data).status_code,302)
        self.assertTrue(ContractAccess.objects.filter(contract=c,user=self.tech).exists())
    def test_cannot_attach_object_to_someone_elses_draft_or_archived_contract(self):
        c=Contract.objects.create(legal_entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID),number='Чужой',subject='Закрытый',work_kinds=['repairs'])
        self.client.force_login(self.tech)
        response=self.client.post(reverse('object_new'),{'name':'Объект','status':'active','related_contract':str(c.pk)})
        self.assertEqual(response.status_code,200);self.assertFalse(ProjectObject.objects.exists())
        self.assertEqual(self.client.get(reverse('object_new')+'?contract='+str(c.pk)).status_code,404)
        c.status='archived';c.save();self.client.force_login(self.leader)
        self.assertEqual(self.client.get(reverse('object_new')+'?contract='+str(c.pk)).status_code,404)
    def test_excel_allows_new_contract_without_objects(self):
        from .registry_excel import headers,parse_rows,commit_preview
        from repairs.excel import workbook
        values=['','EXCEL-FIRST','2026-10-26','Работы','repairs','Заказчик','','active','','','','']
        upload=io.BytesIO(workbook([values],headers=headers(self.leader,'contracts')));upload.name='contracts.xlsx'
        rows=parse_rows(upload,'contracts',self.leader);self.assertFalse(Contract.objects.exists())
        payload={'actor':str(self.leader.pk),'session':self.leader.session_version,'kind':'contracts','rows':rows,'token':str(uuid.uuid4())}
        commit_preview(self.leader,'contracts',payload);self.assertFalse(Contract.objects.get().objects_linked.exists())
