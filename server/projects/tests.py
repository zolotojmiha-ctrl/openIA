import secrets
from decimal import Decimal
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.core.exceptions import ValidationError
from django.test import TestCase, Client
from django.urls import reverse
from accounts.models import User, AuditEvent
from .models import LegalEntity, ProjectObject, Contract, ContractObject, ObjectAccess, ContractAccess, COMPANY_TAX_ID

class ProjectAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        password=secrets.token_urlsafe(24)
        cls.first=User.objects.create_user('+70000000001',password,full_name='ТЕСТ Руководитель 1',leader_slot=1,must_change_password=False)
        cls.second=User.objects.create_user('+70000000002',password,full_name='ТЕСТ Руководитель 2',leader_slot=2,must_change_password=False)
        cls.employee=User.objects.create_user('+70000000003',password,full_name='ТЕСТ Мастер',role='foreman',must_change_password=False)
        cls.other=User.objects.create_user('+70000000004',password,full_name='ТЕСТ Другой сотрудник',role='foreman',must_change_password=False)
        cls.company=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
        cls.foreign=LegalEntity.objects.create(tax_id='0000000000',name='Чужая компания · ТЕСТ')
        cls.a=ProjectObject.objects.create(legal_entity=cls.company,name='Объект А · ТЕСТ',location='Место А')
        cls.b=ProjectObject.objects.create(legal_entity=cls.company,name='СЕКРЕТНЫЙ ОБЪЕКТ Б')
        cls.c=ProjectObject.objects.create(legal_entity=cls.foreign,name='ЧУЖОЕ ЮРЛИЦО')
        cls.contract=Contract.objects.create(legal_entity=cls.company,number='СКРЫТЫЙ-НОМЕР',subject='РАЗРЕШЁННЫЙ ПРЕДМЕТ',counterparty='СКРЫТЫЙ-КОНТРАГЕНТ',amount=Decimal('1234567.89'),private_notes='СЕКРЕТНОЕ-ПРИМЕЧАНИЕ')
        cls.other_contract=Contract.objects.create(legal_entity=cls.company,number='ЧУЖОЙ-ДОГОВОР',subject='ЧУЖОЙ-ПРЕДМЕТ')
        ContractObject.objects.create(contract=cls.contract,project_object=cls.a)
        ContractObject.objects.create(contract=cls.contract,project_object=cls.b)
        ContractObject.objects.create(contract=cls.other_contract,project_object=cls.b)
        ObjectAccess.objects.create(user=cls.employee,project_object=cls.a,granted_by=cls.first)
        ObjectAccess.objects.create(user=cls.other,project_object=cls.b,granted_by=cls.first)
    def as_employee(self):
        self.employee.refresh_from_db();self.client.force_login(self.employee)
    def as_leader(self,second=False):
        self.client.force_login(self.second if second else self.first)
    def grant(self,**kwargs):
        return ContractAccess.objects.create(user=self.employee,contract=self.contract,granted_by=self.first,**kwargs)
    def contract_data(self,**kwargs):
        return {'expected_version':'1','number':'ТЕСТ-001','subject':'Демонстрационные работы','linked_objects':[str(self.a.pk),str(self.b.pk)],'status':'planned',**kwargs}
    def test_anonymous_denied(self):
        for name in ['objects','contracts','object_new','contract_new']:
            self.assertEqual(self.client.get(reverse(name)).status_code,302)
    def test_employee_sees_only_assigned_object(self):
        self.as_employee();r=self.client.get(reverse('objects'))
        self.assertContains(r,self.a.name);self.assertNotContains(r,self.b.name);self.assertNotContains(r,self.c.name)
        self.assertEqual(self.client.get(reverse('object_detail',args=[self.b.pk])).status_code,404)
    def test_search_does_not_find_unassigned_object(self):
        self.as_employee();r=self.client.get(reverse('objects'),{'q':'СЕКРЕТНЫЙ'})
        self.assertEqual(r.context['page'].paginator.count,0)
    def test_object_access_does_not_grant_contract_access(self):
        self.as_employee()
        self.assertEqual(self.client.get(reverse('contract_detail',args=[self.contract.pk])).status_code,404)
        self.assertNotContains(self.client.get(reverse('object_detail',args=[self.a.pk])),self.contract.number)
        self.assertEqual(self.client.get(reverse('contracts')).context['page'].paginator.count,0)
    def test_contract_rule_requires_assigned_object(self):
        ContractAccess.objects.create(user=self.other,contract=self.contract,granted_by=self.first,show_number=True)
        ContractAccess.objects.create(user=self.employee,contract=self.other_contract,granted_by=self.first,show_number=True)
        self.as_employee()
        self.assertEqual(self.client.get(reverse('contract_detail',args=[self.other_contract.pk])).status_code,404)
    def test_allowlisted_fields_only_in_html_and_context(self):
        self.grant(show_subject=True);self.as_employee()
        r=self.client.get(reverse('contract_detail',args=[self.contract.pk]))
        self.assertContains(r,self.contract.subject)
        for secret in [self.contract.number,self.contract.counterparty,self.contract.private_notes,'1234567',self.b.name,self.other.full_name]:
            self.assertNotContains(r,secret)
        self.assertIsNone(r.context.get('contract'))
        self.assertNotIn('amount',r.context['card'])
    def test_hidden_field_search_not_an_oracle(self):
        self.grant(show_subject=True)
        # Another user's broader permissions must not change this employee's search.
        ContractAccess.objects.create(user=self.other,contract=self.contract,granted_by=self.first,show_number=True,show_counterparty=True)
        self.as_employee()
        for term in ['СКРЫТЫЙ-НОМЕР','СКРЫТЫЙ-КОНТРАГЕНТ','СЕКРЕТНОЕ-ПРИМЕЧАНИЕ','1234567']:
            r=self.client.get(reverse('contracts'),{'q':term})
            self.assertEqual(r.context['page'].paginator.count,0)
        self.assertEqual(self.client.get(reverse('contracts'),{'q':'РАЗРЕШЁННЫЙ'}).context['page'].paginator.count,1)
    def test_empty_field_grant_discloses_no_requisites(self):
        self.grant();self.as_employee();r=self.client.get(reverse('contract_detail',args=[self.contract.pk]))
        self.assertEqual(r.context['card']['fields'],[])
        self.assertNotContains(r,self.contract.number)
    def test_employee_cannot_modify_objects_contracts_or_access(self):
        self.as_employee()
        paths=[reverse('object_new'),reverse('object_edit',args=[self.a.pk]),reverse('object_assignments',args=[self.a.pk]),reverse('contract_new'),reverse('contract_edit',args=[self.contract.pk]),reverse('contract_share',args=[self.contract.pk])]
        for path in paths:
            self.assertEqual(self.client.post(path,self.contract_data()).status_code,403)
    def test_both_leaders_have_same_visibility(self):
        for leader in [self.first,self.second]:
            self.client.force_login(leader)
            r=self.client.get(reverse('contract_detail',args=[self.contract.pk]))
            for text in [self.contract.number,self.contract.private_notes,self.b.name]:self.assertContains(r,text)
    def test_many_to_many_without_one_object_limit(self):
        self.as_leader()
        r=self.client.post(reverse('contract_new'),self.contract_data())
        self.assertEqual(r.status_code,302)
        created=Contract.objects.get(number='ТЕСТ-001')
        self.assertEqual(created.objects_linked.count(),2)
        self.assertEqual(self.a.contracts.count(),2)
    def test_other_company_cannot_be_selected_or_viewed(self):
        self.as_leader()
        r=self.client.post(reverse('contract_new'),self.contract_data(linked_objects=[str(self.c.pk)]))
        self.assertEqual(r.status_code,200);self.assertFalse(Contract.objects.filter(number='ТЕСТ-001').exists())
        self.assertEqual(self.client.get(reverse('object_detail',args=[self.c.pk])).status_code,404)
    def test_model_rejects_cross_company_links(self):
        with self.assertRaises(ValidationError):ContractObject.objects.create(contract=self.contract,project_object=self.c)
    def test_both_leaders_create_objects(self):
        for i,leader in enumerate([self.first,self.second]):
            self.client.force_login(leader)
            r=self.client.post(reverse('object_new'),{'name':f'ТЕСТ Новый {i}','status':'planned'})
            self.assertEqual(r.status_code,302)
    def test_assignment_grants_then_revokes_access_and_sessions(self):
        self.grant(show_number=True)
        self.as_employee();employee_session=self.client
        leader=Client();leader.force_login(self.second)
        r=leader.post(reverse('object_assignments',args=[self.a.pk]),{'expected_version':1,'employees':[]})
        self.assertEqual(r.status_code,302)
        self.assertEqual(employee_session.get(reverse('object_detail',args=[self.a.pk])).status_code,302)
        self.assertFalse(ContractAccess.objects.filter(user=self.employee).exists())
        self.as_employee();self.assertEqual(self.client.get(reverse('object_detail',args=[self.a.pk])).status_code,404)
    def test_removing_one_assignment_preserves_grant_if_other_object_remains(self):
        self.grant(show_number=True)
        ObjectAccess.objects.create(user=self.employee,project_object=self.b,granted_by=self.first)
        self.as_leader()
        self.client.post(reverse('object_assignments',args=[self.a.pk]),{'expected_version':1,'employees':[]})
        self.assertTrue(ContractAccess.objects.filter(user=self.employee).exists())
        self.as_employee();self.assertEqual(self.client.get(reverse('contract_detail',args=[self.contract.pk])).status_code,200)
    def test_share_can_only_target_assigned_employees(self):
        self.as_leader()
        r=self.client.post(reverse('contract_share',args=[self.other_contract.pk]),{'expected_version':1,'employee':str(self.employee.pk),'enabled':'on','fields':['show_number']})
        self.assertEqual(r.status_code,200)
        self.assertFalse(ContractAccess.objects.filter(user=self.employee).exists())
    def test_share_saves_and_can_be_revoked(self):
        self.as_leader(second=True)
        url=reverse('contract_share',args=[self.contract.pk])
        self.assertEqual(self.client.post(url,{'expected_version':1,'employee':str(self.employee.pk),'enabled':'on','fields':['show_number','show_subject']}).status_code,302)
        rule=ContractAccess.objects.get(user=self.employee)
        self.assertTrue(rule.show_number);self.assertFalse(rule.show_counterparty)
        self.assertEqual(self.client.post(url,{'expected_version':2,'employee':str(self.employee.pk),'fields':[]}).status_code,302)
        self.assertFalse(ContractAccess.objects.filter(user=self.employee).exists())
    def test_unrecognised_finance_permission_rejected(self):
        self.as_leader();r=self.client.post(reverse('contract_share',args=[self.contract.pk]),{'expected_version':1,'employee':str(self.employee.pk),'enabled':'on','fields':['show_amount']})
        self.assertEqual(r.status_code,200);self.assertFalse(ContractAccess.objects.filter(user=self.employee).exists())
    def test_stale_object_edit_does_not_overwrite(self):
        self.as_leader();url=reverse('object_edit',args=[self.a.pk])
        self.client.post(url,{'expected_version':1,'name':'Первое изменение','status':'planned'})
        r=self.client.post(url,{'expected_version':1,'name':'Устаревшее изменение','status':'planned'})
        self.assertContains(r,'Объект уже изменён');self.a.refresh_from_db();self.assertEqual(self.a.name,'Первое изменение')
    def test_stale_contract_edit_does_not_change_links(self):
        self.as_leader();self.contract.version=2;self.contract.save()
        r=self.client.post(reverse('contract_edit',args=[self.contract.pk]),self.contract_data(linked_objects=[]))
        self.assertContains(r,'Договор уже изменён');self.assertEqual(self.contract.objects_linked.count(),2)
    def test_stale_access_edit_is_rejected(self):
        self.as_leader();self.contract.version=2;self.contract.save()
        r=self.client.post(reverse('contract_share',args=[self.contract.pk]),{'expected_version':1,'employee':str(self.employee.pk),'enabled':'on','fields':['show_number']})
        self.assertContains(r,'Договор или доступ уже изменились');self.assertFalse(ContractAccess.objects.filter(user=self.employee).exists())
    def test_unlinking_last_assigned_object_revokes_contract_rule(self):
        self.grant(show_number=True);self.as_leader()
        self.client.post(reverse('contract_edit',args=[self.contract.pk]),self.contract_data(linked_objects=[str(self.b.pk)]))
        self.assertFalse(ContractAccess.objects.filter(user=self.employee).exists())
    def test_dates_and_negative_amount_rejected(self):
        self.as_leader()
        for extra in [{'amount':'-1'},{'date':'2026-10-18','deadline':'2026-01-01'}]:
            r=self.client.post(reverse('contract_new'),self.contract_data(**extra))
            self.assertEqual(r.status_code,200);self.assertFalse(Contract.objects.filter(number='ТЕСТ-001').exists())
    def test_csrf_required_for_assignments(self):
        client=Client(enforce_csrf_checks=True);client.force_login(self.first)
        self.assertEqual(client.post(reverse('object_assignments',args=[self.a.pk]),{'expected_version':1}).status_code,403)
    def test_search_does_not_duplicate_contracts(self):
        self.grant(show_subject=True);ObjectAccess.objects.create(user=self.employee,project_object=self.b,granted_by=self.first)
        self.as_employee();r=self.client.get(reverse('contracts'),{'q':'РАЗРЕШЁННЫЙ'})
        self.assertEqual(r.context['page'].paginator.count,1)
    def test_malformed_selection_does_not_crash(self):
        self.as_leader()
        self.assertEqual(self.client.get(reverse('contract_share',args=[self.contract.pk]),{'employee':'x'*36}).status_code,200)
    def test_audit_records_data_and_access_changes(self):
        self.as_leader();self.client.post(reverse('object_new'),{'name':'ТЕСТ аудита','status':'planned'})
        self.assertTrue(AuditEvent.objects.filter(action='object_created',details__summary='ТЕСТ аудита').exists())
