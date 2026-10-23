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
    def make_job(self,state='planned'):
        from repairs.models import Section,Job,Defect,JobDefect
        obj=self.contract.objects_linked.get()
        section=Section.objects.create(project_object=obj,rnu='Тест',pipeline='Тест',name='Тест')
        job=Job.objects.create(contract=self.contract,section=section,created_by=self.leader,state=state,method='grind')
        defect=Defect.objects.create(contract=self.contract,section=section,kilometre=1,number='0001')
        JobDefect.objects.create(job=job,defect=defect)
        return job,defect
    def archive(self):
        self.act(self.tech,'request');self.act(self.leader,'archive')
    def test_unfinished_work_blocks_confirmation_but_not_request(self):
        from .closure import closing_checks
        job,defect=self.make_job()
        self.act(self.tech,'request')
        with self.assertRaises(ValidationError):self.act(self.leader,'archive')
        self.contract.refresh_from_db();self.assertEqual(self.contract.status,'active')
        self.assertTrue(closing_checks(self.contract))
        job.state='confirmed';job.save();self.act(self.leader,'archive')
    def test_archive_blocks_all_repair_write_methods(self):
        from repairs.models import Job,Report
        from django.utils import timezone
        job,_=self.make_job('confirmed');self.archive()
        attempts=[lambda:Job.objects.filter(pk=job.pk).update(notes='Изменение'),
                  lambda:job.save(),lambda:Job.objects.bulk_update([job],['notes']),
                  lambda:Report.objects.bulk_create([Report(job=job,author=self.leader,kind='note',performed_date=timezone.localdate())]),
                  lambda:Job.objects.filter(pk=job.pk).delete()]
        for attempt in attempts:
            with self.subTest(attempt=attempt),self.assertRaises(PermissionDenied):attempt()
        self.assertFalse(job.reports.exists())
        self.act(self.leader,'restore');Job.objects.filter(pk=job.pk).update(notes='После восстановления')
        job.refresh_from_db();self.assertEqual(job.notes,'После восстановления')
    def test_reparenting_cannot_bypass_archive(self):
        from repairs.models import Job
        job,_=self.make_job('confirmed');self.archive()
        with self.assertRaises(PermissionDenied):Job.objects.filter(pk=job.pk).update(contract=None)
        job.contract=None
        with self.assertRaises(PermissionDenied):job.save()
        with self.assertRaises(PermissionDenied):Contract.objects.filter(pk=self.contract.pk).update(status='active')
    def test_financial_bulk_import_is_readonly_in_archive(self):
        from finance.models import FinancialScope,PendingWork
        scope=FinancialScope.objects.create(legal_entity=self.contract.legal_entity,contract=self.contract,title='Тест',created_by=self.leader)
        self.archive()
        with self.assertRaises(PermissionDenied):PendingWork.objects.bulk_create([PendingWork(scope=scope,title='Тест',amount=1)])
        self.assertFalse(PendingWork.objects.filter(scope=scope).exists())
    def test_document_content_and_links_frozen_but_access_editable(self):
        from documents.models import Document,DocumentContract,DocumentAccess,DocumentVersion
        doc=Document.objects.create(legal_entity=self.contract.legal_entity,title='Тест',section='contracts',created_by=self.leader)
        DocumentContract.objects.create(contract=self.contract,document=doc)
        self.archive()
        with self.assertRaises(PermissionDenied):Document.objects.filter(pk=doc.pk).update(title='Другое')
        with self.assertRaises(PermissionDenied):DocumentContract.objects.filter(document=doc).delete()
        with self.assertRaises(PermissionDenied):DocumentVersion.objects.create(document=doc,sequence=1,storage_key='test',original_name='test.pdf',content_type='application/pdf',size=1,sha256='0'*64,uploaded_by=self.leader)
        DocumentAccess.objects.create(document=doc,user=self.tech,enabled=False,granted_by=self.leader)
        self.assertTrue(doc.access_rules.filter(user=self.tech).exists())
    def test_partial_object_access_cannot_submit_closure(self):
        obj=ProjectObject.objects.create(legal_entity=self.contract.legal_entity,name='Скрытый объект')
        ContractObject.objects.create(contract=self.contract,project_object=obj)
        with self.assertRaises(PermissionDenied):self.act(self.tech,'request')
    def test_blocking_assignments_and_orders_are_counted(self):
        from .closure import closing_checks
        from .models import Equipment,EquipmentAssignment,Subcontractor,SubcontractOrder
        from django.utils import timezone
        eq=Equipment.objects.create(legal_entity=self.contract.legal_entity,name='Тест',category='Тест',inventory_number='0001',created_by=self.leader)
        placement=EquipmentAssignment.objects.create(equipment=eq,project_object=self.contract.objects_linked.get(),contract=self.contract,start_date=timezone.localdate(),created_by=self.leader)
        company=Subcontractor.objects.create(legal_entity=self.contract.legal_entity,name='Тест',tax_id='0000000001',created_by=self.leader)
        order=SubcontractOrder.objects.create(company=company,contract=self.contract,project_object=placement.project_object,kind='valves',title='Тест',created_by=self.leader)
        self.assertEqual(len(closing_checks(self.contract)),2)
        self.act(self.tech,'request')
        with self.assertRaises(ValidationError):self.act(self.leader,'archive')
        placement.end_date=timezone.localdate();placement.save();order.archived=True;order.save()
        self.act(self.leader,'archive')
    def test_archived_card_remains_readable_without_result_actions(self):
        job,_=self.make_job('confirmed');self.archive();self.client.force_login(self.leader)
        response=self.client.get(reverse('repair_detail',args=[job.pk]))
        self.assertContains(response,'Контракт в архиве')
        self.assertNotContains(response,'Фото / пояснение')
        self.assertEqual(self.client.get(reverse('contract_detail',args=[self.contract.pk])).status_code,200)
    def test_global_pending_notice(self):
        self.act(self.tech,'request');self.client.force_login(self.leader)
        self.assertContains(self.client.get(reverse('contract_detail',args=[self.contract.pk])),'Заявки на закрытие контрактов: 1')

    def test_actual_access_forms_work_in_archive(self):
        from documents.models import Document,DocumentContract,DocumentAccess
        doc=Document.objects.create(legal_entity=self.contract.legal_entity,title='Тест',section='contracts',created_by=self.leader)
        DocumentContract.objects.create(contract=self.contract,document=doc)
        DocumentAccess.objects.create(document=doc,user=self.tech,enabled=True,granted_by=self.leader)
        self.archive();self.client.force_login(self.leader)
        result=self.client.post(reverse('document_share',args=[doc.pk]),{'expected_revision':doc.revision,'employee':self.tech.pk})
        self.assertEqual(result.status_code,302)
        self.assertFalse(doc.access_rules.get(user=self.tech).enabled)
        self.contract.refresh_from_db()
        result=self.client.post(reverse('contract_share',args=[self.contract.pk]),{'expected_version':self.contract.version,'employee':self.tech.pk})
        self.assertEqual(result.status_code,302)
        self.assertFalse(self.contract.access_rules.filter(user=self.tech).exists())
