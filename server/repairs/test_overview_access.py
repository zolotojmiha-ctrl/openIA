"""Read delegated repairs in existing scopes without acquiring report/preparation rights."""
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import PermissionDenied
from accounts.models import AccessGrant
from projects.models import ObjectAccess,ContractAccess
from . import tests as fixture
from .access import visible_jobs,reportable_jobs,can_prepare
from .services import report_work
from .models import Report

class RepairOverviewTests(TestCase):
    setUpTestData=classmethod(fixture.RepairsTests.setUpTestData.__func__)
    setUp=fixture.RepairsTests.setUp
    payload=fixture.RepairsTests.payload

    def delegate(self):return AccessGrant.objects.create(user=self.worker,permission='repairs.overview',granted_by=self.leader)
    def move_other_into_scope(self):
        self.hidden.section=self.section;self.hidden.contract=self.contract;self.hidden.save()
        defect=self.hidden.defects.get();defect.section=self.section;defect.contract=self.contract;defect.save()

    def test_grant_opens_other_masters_job_but_never_hidden_object(self):
        self.assertNotIn(self.hidden,visible_jobs(self.worker))
        self.delegate();self.assertNotIn(self.hidden,visible_jobs(self.worker))
        self.move_other_into_scope()
        self.assertIn(self.hidden,visible_jobs(self.worker))
        self.assertContains(self.client.get(reverse('repair_detail',args=[self.hidden.pk])),'999999')
        self.assertContains(self.client.get(reverse('repairs')),'Все дефекты доступных участков')
        self.assertContains(self.client.get(reverse('dashboard')),'Все дефекты доступных участков')

    def test_object_and_contract_access_remain_required(self):
        self.delegate();self.move_other_into_scope()
        ContractAccess.objects.filter(user=self.worker,contract=self.contract).delete()
        self.assertFalse(visible_jobs(self.worker).filter(pk=self.hidden.pk).exists())
        ContractAccess.objects.create(user=self.worker,contract=self.contract,granted_by=self.leader)
        ObjectAccess.objects.filter(user=self.worker,project_object=self.obj).delete()
        self.assertFalse(visible_jobs(self.worker).filter(pk=self.hidden.pk).exists())

    def test_reading_never_grants_report_photo_start_problem_or_preparation(self):
        self.delegate();self.move_other_into_scope()
        self.assertFalse(can_prepare(self.worker))
        self.assertNotIn(self.hidden,reportable_jobs(self.worker));self.assertIn(self.job,reportable_jobs(self.worker))
        for action in ('start','result','note','progress','problem','repaired','unrepaired'):
            with self.subTest(action=action):
                self.assertEqual(self.client.get(reverse('repair_report',args=[self.hidden.pk,action])).status_code,404)
                self.assertEqual(self.client.post(reverse('repair_report',args=[self.hidden.pk,action]),self.payload()).status_code,404)
        with self.assertRaises(PermissionDenied):report_work(self.worker,self.hidden.pk,'start',{})
        self.assertEqual(Report.objects.count(),0)
        self.assertEqual(self.client.get(reverse('repair_edit',args=[self.hidden.pk])).status_code,403)
        self.hidden.refresh_from_db();self.assertEqual(self.hidden.assigned_to_id,self.other.pk)

    def test_revocation_and_subcontractor_role_keep_existing_boundaries(self):
        grant=self.delegate();self.move_other_into_scope()
        self.assertIn(self.hidden,visible_jobs(self.worker));grant.delete()
        self.assertNotIn(self.hidden,visible_jobs(self.worker))
        self.delegate();self.worker.role='subcontractor';self.worker.save(update_fields=['role'])
        self.assertNotIn(self.hidden,visible_jobs(self.worker))

    def test_no_default_grants_and_leader_can_delegate_via_existing_employee_form(self):
        from accounts.forms import EmployeeForm
        from accounts.presets import PRESETS
        self.assertTrue(all('repairs.overview' not in p['permissions'] for p in PRESETS.values()))
        form=EmployeeForm(user=self.worker,actor=self.leader)
        self.assertIn('repairs.overview',dict(form.fields['permissions'].choices))
        form=EmployeeForm(user=self.other,actor=self.worker)
        self.assertNotIn('repairs.overview',dict(form.fields['permissions'].choices))
