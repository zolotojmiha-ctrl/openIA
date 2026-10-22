from django.test import TestCase
from django.core.exceptions import PermissionDenied,ValidationError
from django.urls import reverse
from accounts.models import User,AccessGrant
from .models import LegalEntity,Contract,ProjectObject,ContractObject,ObjectAccess,ContractAccess,COMPANY_TAX_ID
from .closure import transition
from .forms import ContractForm

class ClosureTests(TestCase):
    def setUp(self):
        self.leader=User.objects.create_user('+79990009001','test-secret',full_name='Руководитель',leader_slot=1,must_change_password=False)
        self.tech=User.objects.create_user('+79990009002','test-secret',full_name='Техник',role='technician',must_change_password=False)
        company=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
        self.contract=Contract.objects.create(legal_entity=company,number='Тест',subject='Работы',status='active')
        obj=ProjectObject.objects.create(legal_entity=company,name='Объект')
        ContractObject.objects.create(contract=self.contract,project_object=obj)
        ObjectAccess.objects.create(user=self.tech,project_object=obj,granted_by=self.leader)
        ContractAccess.objects.create(user=self.tech,contract=self.contract,granted_by=self.leader)
        AccessGrant.objects.create(user=self.tech,permission='projects.manage',granted_by=self.leader)
    def act(self,user,action,version=None,note='Пояснение'):
        self.contract.refresh_from_db()
        return transition(user,self.contract.pk,version or self.contract.version,action,note)
    def test_roundtrip_preserves_access_and_history(self):
        self.act(self.tech,'request');self.act(self.leader,'return')
        self.act(self.tech,'request');self.act(self.leader,'archive')
        self.contract.refresh_from_db();self.assertEqual(self.contract.status,'archived')
        self.act(self.leader,'restore');self.contract.refresh_from_db()
        self.assertEqual(self.contract.status,'active');self.assertEqual(self.contract.closure_events.count(),5)
        self.assertTrue(self.contract.access_rules.filter(user=self.tech).exists())
    def test_technician_cannot_confirm(self):
        self.act(self.tech,'request')
        with self.assertRaises(PermissionDenied):self.act(self.tech,'archive')
    def test_stale_request_and_empty_reason(self):
        self.act(self.tech,'request')
        with self.assertRaises(ValidationError):self.act(self.leader,'archive',version=1)
        with self.assertRaises(ValidationError):self.act(self.leader,'return',note=' ')
        self.assertEqual(self.contract.closure_events.count(),1)
    def test_no_direct_archive_choice(self):
        self.assertNotIn('archived',dict(ContractForm(user=self.leader).fields['status'].choices))
    def test_revoked_rights(self):
        self.tech.grants.all().delete()
        with self.assertRaises(PermissionDenied):self.act(self.tech,'request')
    def test_pages_and_pending_queue(self):
        self.act(self.tech,'request');self.client.force_login(self.leader)
        self.assertContains(self.client.get(reverse('contracts')),'Рассмотреть: Тест')
        self.assertContains(self.client.get(reverse('contract_detail',args=[self.contract.pk])),'ожидает решения')
        self.assertEqual(self.client.get(reverse('contract_closure',args=[self.contract.pk])).status_code,200)
