from django.test import TestCase
from django.urls import reverse
from . import tests as fixture
from .models import User,AccessGrant,ROLES,PERMISSIONS
from .presets import PRESETS
from .forms import EmployeeForm

class PermissionPresetTests(TestCase):
    setUpTestData=classmethod(fixture.AccountTests.setUpTestData.__func__)
    def payload(self,role='foreman',**extra):
        return {'full_name':'ТЕСТ Набор','phone':'+70000000990','role':role,'profile':role,'is_active':'on',**extra}
    def test_every_role_has_a_valid_profile_and_role_sets_stay_bounded(self):
        self.assertEqual(set(PRESETS),{key for key,label in ROLES})
        for role,preset in PRESETS.items():
            with self.subTest(role=role):
                self.assertLessEqual(set(preset['permissions']),set(PERMISSIONS))
                self.assertNotIn('users.manage',preset['permissions']);self.assertNotIn('projects.access',preset['permissions'])
                if role!='accounting':self.assertFalse(set(preset['permissions'])&{'finance.read','finance.write'})
        self.assertIn('repairs.report_scope',PRESETS['foreman']['permissions'])
        self.assertEqual(PRESETS['customer']['permissions'],[])
        self.assertEqual(PRESETS['subcontractor']['permissions'],[])
        self.assertEqual(PRESETS['other']['permissions'],[])
    def test_all_profiles_apply_on_server_when_disabled_checkboxes_are_absent(self):
        for role,preset in PRESETS.items():
            with self.subTest(role=role):
                form=EmployeeForm(self.payload(role),actor=self.first)
                self.assertTrue(form.is_valid(),form.errors)
                self.assertEqual(set(form.cleaned_data['permissions']),set(preset['permissions']))
    def test_new_form_initial_profile_and_checkbox_preview_match(self):
        for role,preset in PRESETS.items():
            form=EmployeeForm(initial={'role':role},actor=self.first)
            self.assertEqual(form.initial['profile'],role)
            self.assertEqual(set(form.initial['permissions']),set(preset['permissions']))
        self.assertFalse(AccessGrant.objects.exists())
    def test_existing_master_open_and_plain_save_never_add_scope_permission(self):
        self.client.force_login(self.first)
        url=reverse('employee_edit',args=[self.employee.pk]);response=self.client.get(url)
        self.assertEqual(response.context['form'].initial['profile'],'custom')
        self.assertNotIn('repairs.report_scope',response.context['form'].initial['permissions'])
        payload=self.payload(phone=self.employee.phone,profile='custom',expected_version=self.employee.session_version)
        self.assertEqual(self.client.post(url,payload).status_code,302)
        self.assertFalse(self.employee.grants.exists())
    def test_existing_profile_inference_preserves_manual_extras(self):
        for key in ('repairs.report_scope','document.export'):
            AccessGrant.objects.create(user=self.employee,permission=key,granted_by=self.first)
        form=EmployeeForm(user=self.employee,actor=self.first)
        self.assertEqual(form.initial['profile'],'foreman')
        self.assertEqual(set(form.initial['permissions']),{'repairs.report_scope','document.export'})
    def test_custom_mode_remains_explicit_and_mismatched_profiles_are_rejected(self):
        form=EmployeeForm(self.payload(profile='custom',permissions=['document.upload']),actor=self.first)
        self.assertTrue(form.is_valid(),form.errors);self.assertEqual(form.cleaned_data['permissions'],['document.upload'])
        form=EmployeeForm(self.payload(profile='pto'),actor=self.first)
        self.assertFalse(form.is_valid());self.assertIn('не соответствует',str(form.errors))
    def test_delegated_editor_cannot_choose_an_unheld_role_profile(self):
        AccessGrant.objects.create(user=self.employee,permission='users.manage',granted_by=self.first)
        form=EmployeeForm(actor=self.employee)
        self.assertNotIn('foreman',dict(form.fields['profile'].choices))
        self.assertNotIn('customer',dict(form.fields['profile'].choices))
        form=EmployeeForm(self.payload(),actor=self.employee)
        self.assertFalse(form.is_valid())
    def test_profile_and_manual_extras_persist_only_for_saved_employee(self):
        self.client.force_login(self.first)
        payload=self.payload(permissions=['document.export'])
        self.assertEqual(self.client.post(reverse('employee_new'),payload).status_code,200)
        created=User.objects.get(phone=payload['phone'])
        self.assertEqual(set(created.grants.values_list('permission',flat=True)),{'repairs.report_scope','document.export'})
        self.assertFalse(self.employee.grants.exists())
    def test_existing_accounting_custom_finance_grant_is_not_replaced_on_open(self):
        self.employee.role='accounting';self.employee.save(update_fields=['role'])
        AccessGrant.objects.create(user=self.employee,permission='finance.read',granted_by=self.first)
        form=EmployeeForm(user=self.employee,actor=self.first)
        self.assertEqual(form.initial['profile'],'custom')
        self.assertEqual(form.initial['permissions'],['finance.read'])
        self.assertEqual(set(self.employee.grants.values_list('permission',flat=True)),{'finance.read'})

    def test_customer_does_not_receive_internal_permissions_from_tampered_form(self):
        form=EmployeeForm(self.payload('customer',permissions=['document.upload']),actor=self.first)
        self.assertFalse(form.is_valid())
