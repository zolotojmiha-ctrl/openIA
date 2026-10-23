from django.test import TestCase
from django.urls import reverse
from .models import User, AccessGrant
from .start import TITLES, role_home


class RoleHomeTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user('+70000000981','test-secret',full_name='ТЕСТ сотрудник',must_change_password=False)

    def test_every_role_renders_without_granting_management(self):
        for role,(title,_) in TITLES.items():
            if role=='leader':continue
            with self.subTest(role=role):
                self.user.role=role;self.user.save()
                self.client.force_login(self.user)
                response=self.client.get(reverse('dashboard'))
                self.assertContains(response,title)
                self.assertEqual(response.context['role_home']['role'],role)
                self.assertEqual(self.client.get(reverse('users')).status_code,403)
                self.assertNotContains(response,'Финансовая сводка')

    def test_leader_priority_and_query_cannot_switch_role(self):
        self.user.leader_slot=1;self.user.save()
        self.client.force_login(self.user)
        response=self.client.get(reverse('dashboard'),{'role':'foreman'})
        self.assertContains(response,'Проверки и сводка объектов')
        self.assertFalse(response.context['role_home']['common_collapsed'])

    def test_delegated_powers_remain_available_without_changing_role(self):
        self.user.role='foreman';self.user.save()
        AccessGrant.objects.create(user=self.user,permission='equipment.manage',granted_by=self.user)
        result=role_home(self.user)
        self.assertEqual(result['role'],'foreman')
        self.assertIn(reverse('equipment'),[x['url'] for x in result['extras']])
        self.assertEqual(result['rows'],[])

    def test_supply_and_mechanic_action_links(self):
        for role,grant,name in [('supply','stock.manage','stock_new'),('mechanic','equipment.manage','equipment_new')]:
            self.user.role=role;self.user.save()
            AccessGrant.objects.get_or_create(user=self.user,permission=grant,defaults={'granted_by':self.user})
            result=role_home(self.user)
            self.assertIn(reverse(name),[x['url'] for x in result['cards']])
        self.assertIn(reverse('equipment')+'?state=repair',[x['url'] for x in result['cards']])

    def test_inactive_has_no_start_screen(self):
        self.user.is_active=False
        self.assertIsNone(role_home(self.user))

    def test_foreman_prepare_grant_does_not_mix_other_masters_tasks(self):
        from projects.models import LegalEntity,ProjectObject,Contract,ContractObject,ObjectAccess,ContractAccess,COMPANY_TAX_ID
        from repairs.models import Section,Job,Defect,JobDefect
        company=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
        obj=ProjectObject.objects.create(legal_entity=company,name='ТЕСТ объект')
        contract=Contract.objects.create(legal_entity=company,number='ТЕСТ',work_kinds=['repairs'])
        ContractObject.objects.create(contract=contract,project_object=obj)
        ObjectAccess.objects.create(user=self.user,project_object=obj,granted_by=self.user)
        ContractAccess.objects.create(user=self.user,contract=contract,granted_by=self.user)
        section=Section.objects.create(project_object=obj,rnu='ТЕСТ',pipeline='ТЕСТ',name='Участок')
        self.user.role='foreman';self.user.save()
        AccessGrant.objects.create(user=self.user,permission='repairs.prepare',granted_by=self.user)
        for number,assigned in [('001',self.user),('999',None)]:
            job=Job.objects.create(contract=contract,section=section,assigned_to=assigned,method='grind',created_by=self.user)
            defect=Defect.objects.create(contract=contract,section=section,kilometre='100.123',number=number,pipe_section='0001')
            JobDefect.objects.create(job=job,defect=defect)
        result=role_home(self.user)
        self.assertEqual(len(result['rows']),1)
        self.assertIn('001',result['rows'][0]['title'])
        self.assertNotIn('999',result['rows'][0]['title'])
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('dashboard')),'Внести результат')

    def test_safety_role_does_not_reveal_unshared_expiry_documents(self):
        from documents.models import Document
        from projects.models import LegalEntity,COMPANY_TAX_ID
        from django.utils import timezone
        owner=User.objects.create_user('+70000000982','test-secret',full_name='ТЕСТ владелец',must_change_password=False)
        Document.objects.create(legal_entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID),title='Закрытый медосмотр',section='personnel',created_by=owner,expires_on=timezone.localdate(),remind=True)
        self.user.role='safety';self.user.save()
        self.client.force_login(self.user)
        response=self.client.get(reverse('dashboard'))
        self.assertNotContains(response,'Закрытый медосмотр')
        self.assertEqual(response.context['role_home']['due_count'],0)
