import uuid,secrets,tempfile
from decimal import Decimal
from django.test import TestCase,override_settings
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import ValidationError,PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from accounts.models import User,AccessGrant,AuditEvent
from documents.models import Document,DocumentAccess
from repairs.models import Job,Section,Defect,JobDefect,Report
from repairs.access import visible_jobs,reportable_jobs
from repairs.services import report_work
from repairs.excel import workbook
from .models import LegalEntity,ProjectObject,Contract,ContractObject,ObjectAccess,ContractAccess,COMPANY_TAX_ID,Subcontractor,SubcontractRepresentative,SubcontractOrder,SubcontractOrderAccess,SubcontractDocument,SubcontractResult,SubcontractAttachment
from . import subcontracts as s,subcontract_excel as excel

class SubcontractTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        pwd=secrets.token_urlsafe(24)
        cls.leader=User.objects.create_user('+70000000001',pwd,leader_slot=1,full_name='ТЕСТ Руководитель',must_change_password=False)
        cls.tech=User.objects.create_user('+70000000002',pwd,role='technician',full_name='ТЕСТ Техник',must_change_password=False)
        cls.rep=User.objects.create_user('+70000000003',pwd,role='subcontractor',full_name='ТЕСТ Представитель',must_change_password=False)
        cls.other=User.objects.create_user('+70000000004',pwd,role='subcontractor',full_name='ТЕСТ Чужой',must_change_password=False)
        cls.entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID);cls.obj=ProjectObject.objects.create(legal_entity=cls.entity,name='ТЕСТ НПС Татарская')
        cls.contract=Contract.objects.create(legal_entity=cls.entity,number='СЕКРЕТ-1726',subject='Закрытый предмет',work_kinds=['repairs','valves','diagnostics'])
        ContractObject.objects.create(contract=cls.contract,project_object=cls.obj)
        for user in (cls.rep,cls.other,cls.tech):
            ObjectAccess.objects.create(user=user,project_object=cls.obj,granted_by=cls.leader);ContractAccess.objects.create(user=user,contract=cls.contract,granted_by=cls.leader)
        cls.section=Section.objects.create(project_object=cls.obj,rnu='ТЕСТ',pipeline='ТЕСТ',name='ТЕСТ Участок')
    def company(self,tax='0000000001',name='ТЕСТ Монтаж'):return s.save_company(self.leader,None,None,dict(name=name,tax_id=tax,contact='ТЕСТ контакт',notes='Внутреннее примечание',archived=False))
    def order_data(self,company,**changes):return dict(company=company,contract=self.contract,project_object=self.obj,kind='valves',title='ТЕСТ Замена УЗА',scope='ТЕСТ Участок работ',planned_quantity=Decimal(2),unit='шт.',responsible=self.tech,archived=False)|changes
    def order(self,company=None,**changes):return s.save_order(self.leader,None,None,self.order_data(company or self.company(),**changes))
    def grant(self,order,report=True,user=None):
        user=user or self.rep;rep=s.save_representative(self.leader,order.company_id,None,None,user,True);access=s.grant_order(self.leader,order.pk,rep.pk,True,report);return rep,access
    def data(self,order,**changes):return dict(token=uuid.uuid4(),expected_version=order.version,outcome='complete',quantity=Decimal(2),date=timezone.localdate(),note='ТЕСТ Выполнено',files=[])|changes
    def job(self):
        j=Job.objects.create(contract=self.contract,section=self.section,method='grind',created_by=self.leader)
        d=Defect.objects.create(contract=self.contract,section=self.section,kilometre=1,number=str(Job.objects.count()));JobDefect.objects.create(job=j,defect=d);return j
    def doc(self):return Document.objects.create(legal_entity=self.entity,title='ТЕСТ Договор субподряда',section='contracts',kind='subcontractor',created_by=self.leader)
    def test_role_does_not_grant_management(self):
        self.client.force_login(self.tech);self.assertEqual(self.client.get(reverse('subcontract_company_new')).status_code,403)
        with self.assertRaises(PermissionDenied):s.save_company(self.tech,None,None,dict(name='X',tax_id='0000000001'))
        AccessGrant.objects.create(user=self.tech,permission='subcontract.manage',granted_by=self.leader);self.assertEqual(self.client.get(reverse('subcontract_company_new')).status_code,200)
    def test_company_tax_id_unique_and_old_versions_fail(self):
        company=self.company()
        with self.assertRaises(ValidationError):self.company()
        with self.assertRaises(ValidationError):self.company(tax='letters')
        with self.assertRaises(ValidationError):s.save_company(self.leader,company.pk,999,dict(name='Тест',tax_id=company.tax_id))
    def test_order_validates_contract_kind_and_object(self):
        company=self.company();obj=ProjectObject.objects.create(legal_entity=self.entity,name='Другой')
        with self.assertRaises(ValidationError):self.order(company,project_object=obj)
        with self.assertRaises(ValidationError):self.order(company,kind='construction')
        with self.assertRaises(ValidationError):self.order(company,planned_quantity=Decimal(-1))
    def test_membership_alone_does_not_open_orders(self):
        order=self.order();s.save_representative(self.leader,order.company_id,None,None,self.rep,True)
        self.assertFalse(s.orders(self.rep).exists());self.assertFalse(s.companies(self.rep).exists())
    def test_explicit_access_is_only_to_selected_order(self):
        first=self.order();second=self.order(first.company,title='СЕКРЕТ Другое поручение');self.grant(first)
        self.assertEqual(list(s.orders(self.rep)),[first]);self.client.force_login(self.rep)
        response=self.client.get(reverse('subcontracts'));self.assertNotContains(response,second.title);self.assertNotContains(response,self.contract.number)
        self.assertEqual(self.client.get(reverse('subcontract_order',args=[second.pk])).status_code,404)
    def test_other_company_cannot_access_order(self):
        first=self.order();second=self.order(self.company('0000000002','СЕКРЕТ Другая компания'));self.grant(first);self.grant(second,user=self.other)
        self.client.force_login(self.other);self.assertEqual(self.client.get(reverse('subcontract_order',args=[first.pk])).status_code,404)
    def test_access_needs_parent_grants_and_never_creates_them(self):
        order=self.order();rep=s.save_representative(self.leader,order.company_id,None,None,self.rep,True);ContractAccess.objects.filter(user=self.rep).delete()
        with self.assertRaises(ValidationError):s.grant_order(self.leader,order.pk,rep.pk,True,True)
        self.assertFalse(ContractAccess.objects.filter(user=self.rep).exists());self.assertFalse(DocumentAccess.objects.exists())
    def test_read_only_representative_cannot_submit(self):
        order=self.order();self.grant(order,report=False)
        with self.assertRaises(PermissionDenied):s.submit_result(self.rep,order.pk,self.data(order))
        self.client.force_login(self.rep);self.assertEqual(self.client.get(reverse('subcontract_result',args=[order.pk])).status_code,404)
    def test_inactive_membership_and_account_remove_access(self):
        order=self.order();rep,grant=self.grant(order)
        s.save_representative(self.leader,order.company_id,rep.pk,rep.version,self.rep,False);self.assertFalse(s.orders(self.rep).exists())
        rep.refresh_from_db();s.save_representative(self.leader,order.company_id,rep.pk,rep.version,self.rep,True);self.rep.is_active=False;self.rep.save()
        self.assertFalse(s.can_report(self.rep,order));self.rep.is_active=True;self.rep.save()
    def test_internal_responsible_can_report_without_company_admin(self):
        order=self.order();result=s.submit_result(self.tech,order.pk,self.data(order));self.assertEqual(result.author,self.tech)
    def test_result_author_qty_retry_and_pending_notice(self):
        order=self.order();self.grant(order);data=self.data(order);first=s.submit_result(self.rep,order.pk,data);again=s.submit_result(self.rep,order.pk,data)
        self.assertEqual(first.pk,again.pk);self.assertEqual(first.author,self.rep);self.assertEqual(SubcontractResult.objects.count(),1)
        self.client.force_login(self.leader);self.assertContains(self.client.get(reverse('dashboard')),'Выполнение субподрядчиков на проверке: 1')
        with self.assertRaises(ValidationError):s.submit_result(self.rep,order.pk,{**data,'note':'Изменено'})
    def test_partial_quantities_are_cumulative_not_summed(self):
        order=self.order();self.grant(order);s.submit_result(self.rep,order.pk,self.data(order,outcome='partial',quantity=Decimal(1)));order.refresh_from_db();s.submit_result(self.rep,order.pk,self.data(order,quantity=Decimal(2)))
        self.assertEqual(order.results.first().quantity,2);self.assertEqual(order.results.count(),2)
    def test_return_and_correction_keep_original_results(self):
        order=self.order();self.grant(order);first=s.submit_result(self.rep,order.pk,self.data(order));order.refresh_from_db()
        with self.assertRaises(ValidationError):s.decide(self.leader,order.pk,order.version,'revision','')
        s.decide(self.leader,order.pk,order.version,'revision','Уточните объём');order.refresh_from_db();self.client.force_login(self.rep);self.assertContains(self.client.get(reverse('dashboard')),'Поручения вернули на доработку: 1');second=s.submit_result(self.rep,order.pk,self.data(order,quantity=Decimal('1.5')));order.refresh_from_db()
        corrected=s.submit_result(self.leader,order.pk,self.data(order,note='Исправлено по акту',quantity=Decimal(2)),accept=True)
        self.assertEqual(first.author,self.rep);self.assertEqual(first.decision.decision,'revision');self.assertTrue(corrected.correction);self.assertEqual(corrected.decision.decision,'confirmed');self.assertEqual(order.results.count(),3)
    def test_nonleader_cannot_confirm_or_correct_accept(self):
        order=self.order();self.grant(order);s.submit_result(self.rep,order.pk,self.data(order));order.refresh_from_db()
        with self.assertRaises(PermissionDenied):s.decide(self.rep,order.pk,order.version,'confirmed')
        with self.assertRaises(PermissionDenied):s.submit_result(self.rep,order.pk,self.data(order),True)
    def test_generic_result_on_repair_order_rejected(self):
        order=self.order(kind='repairs');self.grant(order)
        with self.assertRaises(ValidationError):s.submit_result(self.rep,order.pk,self.data(order))
    def test_repair_access_is_explicit_and_report_grant_is_rechecked(self):
        order=self.order(kind='repairs');rep,grant=self.grant(order);a=self.job();b=self.job();s.link_repairs(self.leader,order.pk,{str(a.pk):a.version})
        self.assertEqual(list(visible_jobs(self.rep)),[a]);self.assertFalse(reportable_jobs(self.other).exists())
        a.refresh_from_db();data=dict(token=uuid.uuid4(),expected_version=a.version,performed_date=timezone.localdate(),outcome='complete',method='grind',note='ТЕСТ',couplings=[],files=[])
        result=report_work(self.rep,a.pk,'result',data);self.assertEqual(result.author,self.rep);self.assertEqual(result.context['subcontract']['company'],str(order.company_id))
        s.grant_order(self.leader,order.pk,rep.pk,True,False,grant.version);self.assertFalse(reportable_jobs(self.rep).exists());self.assertTrue(visible_jobs(self.rep).exists())
    def test_linking_wrong_contract_is_atomic(self):
        order=self.order(kind='repairs');a=self.job();b=self.job();other=Contract.objects.create(legal_entity=self.entity,number='ДРУГОЙ',subject='ТЕСТ',work_kinds=['repairs']);b.contract=other;b.save()
        with self.assertRaises(ValidationError):s.link_repairs(self.leader,order.pk,{str(a.pk):a.version,str(b.pk):b.version})
        a.refresh_from_db();self.assertIsNone(a.subcontract_order_id)
    def test_unlink_revokes_task_and_preserves_history(self):
        order=self.order(kind='repairs');self.grant(order);a=self.job();s.link_repairs(self.leader,order.pk,{str(a.pk):a.version});a.refresh_from_db();s.unlink_repair(self.leader,order.pk,a.pk,a.version,'Ошибочное назначение')
        self.assertFalse(visible_jobs(self.rep).exists());self.assertEqual(a.reports.filter(kind='assignment').count(),2)
    def test_documents_need_separate_file_permission(self):
        order=self.order();self.grant(order);doc=self.doc();link=s.save_document(self.leader,order.company_id,None,None,dict(order=order,document=doc,label='Договор',active=True))
        self.assertFalse(s.files(self.rep).exists());self.assertFalse(DocumentAccess.objects.exists())
        DocumentAccess.objects.create(document=doc,user=self.rep,enabled=True,granted_by=self.leader);self.assertEqual(s.files(self.rep).get().pk,link.pk)
        s.save_document(self.leader,order.company_id,link.pk,link.version,dict(order=order,document=doc,label='Договор',active=False));self.assertFalse(s.files(self.rep).exists())
    def test_order_scope_and_unit_cannot_reinterpret_history(self):
        order=self.order();self.grant(order);s.submit_result(self.rep,order.pk,self.data(order));order.refresh_from_db()
        with self.assertRaises(ValidationError):s.save_order(self.leader,order.pk,order.version,self.order_data(order.company,unit='м'))
        with self.assertRaises(ValidationError):s.save_order(self.leader,order.pk,order.version,self.order_data(order.company,kind='diagnostics'))
    def test_files_private_and_denied_after_revocation(self):
        from documents.storage import validate_upload
        order=self.order();rep,grant=self.grant(order);file=validate_upload(SimpleUploadedFile('ТЕСТ.txt','Синтетический файл'.encode(),content_type='text/plain'))
        with tempfile.TemporaryDirectory() as root,override_settings(PRIVATE_DOCUMENT_ROOT=root):
            result=s.submit_result(self.rep,order.pk,self.data(order,files=[file]));attachment=result.attachments.get();self.client.force_login(self.rep)
            response=self.client.get(reverse('subcontract_attachment',args=[attachment.pk]));self.assertEqual(response.status_code,200);self.assertEqual(b''.join(response.streaming_content),'Синтетический файл'.encode())
            s.grant_order(self.leader,order.pk,rep.pk,False,False,grant.version);self.assertEqual(self.client.get(reverse('subcontract_attachment',args=[attachment.pk])).status_code,404)
    def test_archiving_requires_finish_and_retains_results(self):
        order=self.order();self.grant(order);s.submit_result(self.rep,order.pk,self.data(order));order.refresh_from_db()
        with self.assertRaises(ValidationError):s.save_order(self.leader,order.pk,order.version,self.order_data(order.company,archived=True))
        s.decide(self.leader,order.pk,order.version,'confirmed');order.refresh_from_db();s.save_order(self.leader,order.pk,order.version,self.order_data(order.company,archived=True))
        self.assertFalse(s.orders(self.rep).exists());self.assertEqual(order.results.count(),1)
    def book(self,kind,rows):return SimpleUploadedFile('subcontract.xlsx',workbook(rows,headers=excel.HEADERS[kind]))
    def test_xlsx_round_trip_all_scoped_registries(self):
        order=self.order();self.grant(order);doc=self.doc();s.save_document(self.leader,order.company_id,None,None,dict(order=order,document=doc,label='Договор',active=True))
        for kind in ('companies','orders','representatives','access','documents'):
            rows=excel.parse_rows(self.book(kind,excel.export_rows(self.leader,kind)),kind,self.leader)
            with transaction.atomic():excel.apply_rows(self.leader,kind,rows)
        order.refresh_from_db();s.submit_result(self.rep,order.pk,self.data(order));rows=excel.parse_rows(self.book('results',excel.export_rows(self.leader,'results')),'results',self.leader)
        with transaction.atomic():excel.apply_rows(self.leader,'results',rows)
        self.assertEqual(SubcontractResult.objects.count(),1)
    def test_xlsx_preview_rollback_and_safe_apply_retry(self):
        self.client.force_login(self.leader);url=reverse('subcontract_import',args=['companies']);r=self.client.post(url,{'file':self.book('companies',[['','ТЕСТ Импорт','0000000010','','','0','']])})
        self.assertEqual(r.status_code,200);self.assertFalse(Subcontractor.objects.exists());token=r.context['preview'];self.assertTrue(token)
        for _ in range(2):self.assertEqual(self.client.post(url,{'apply':'1','preview':token}).status_code,302)
        self.assertEqual(Subcontractor.objects.count(),1)
    def test_xlsx_bad_later_row_rolls_back_company(self):
        rows=[{'row':2,'values':['','ТЕСТ','0000000010','','','0','']},{'row':3,'values':['','Плохой','bad','','','0','']}]
        with self.assertRaises(ValidationError):
            with transaction.atomic():excel.apply_rows(self.leader,'companies',rows)
        self.assertFalse(Subcontractor.objects.exists())
    def test_company_document_upload_links_without_grant(self):
        company=self.company();self.client.force_login(self.leader)
        with tempfile.TemporaryDirectory() as root,override_settings(PRIVATE_DOCUMENT_ROOT=root):
            r=self.client.post(reverse('document_upload')+'?subcontractor='+str(company.pk),dict(file=SimpleUploadedFile('ТЕСТ.txt',b'Synthetic'),section='contracts',collision='ask',defer_link='on',upload_token=str(uuid.uuid4())),HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(r.status_code,200,r.content);link=SubcontractDocument.objects.get(company=company);self.assertFalse(DocumentAccess.objects.filter(document=link.document).exists())
    def test_contract_fields_and_internal_company_notes_hidden(self):
        order=self.order();self.grant(order);self.client.force_login(self.rep)
        response=self.client.get(reverse('subcontract_order',args=[order.pk]));self.assertNotContains(response,self.contract.number);self.assertNotContains(response,self.contract.subject)
        self.assertNotContains(self.client.get(reverse('subcontract_company',args=[order.company_id])),'Внутреннее примечание')

    def test_external_cannot_choose_another_tasks_coupling(self):
        from repairs.models import Coupling
        from repairs.forms import ReportForm
        order=self.order(kind='repairs');self.grant(order);job=self.job();s.link_repairs(self.leader,order.pk,{str(job.pk):job.version});job.refresh_from_db()
        coupling=Coupling.objects.create(project_object=self.obj,method='p2',length_mm=1000,wall_mm=8,serial='ЧУЖАЯ МУФТА')
        form=ReportForm(job=job,action='result',user=self.rep);self.assertFalse(form.fields['couplings'].queryset.filter(pk=coupling.pk).exists())
        with self.assertRaises(PermissionDenied):report_work(self.rep,job.pk,'result',dict(token=uuid.uuid4(),expected_version=job.version,performed_date=timezone.localdate(),outcome='complete',method='p2',length_m=1,note='ТЕСТ',couplings=[coupling],files=[]))
        self.assertFalse(job.reports.exclude(kind='assignment').exists())
    def test_service_denies_submission_after_order_access_is_revoked(self):
        order=self.order();rep,grant=self.grant(order);s.grant_order(self.leader,order.pk,rep.pk,False,False,grant.version)
        with self.assertRaises(PermissionDenied):s.submit_result(self.rep,order.pk,self.data(order))
        self.assertFalse(order.results.exists())

    def test_delegate_can_share_only_owned_linked_documents_with_representatives(self):
        from documents.forms import ShareForm
        order=self.order();rep,grant=self.grant(order);AccessGrant.objects.create(user=self.tech,permission='subcontract.manage',granted_by=self.leader)
        doc=self.doc();doc.created_by=self.tech;doc.save();unrelated=Document.objects.create(legal_entity=self.entity,title='СЕКРЕТ Прочий документ',section='general',created_by=self.tech)
        s.save_document(self.leader,order.company_id,None,None,dict(order=order,document=doc,label='ТЕСТ',active=True))
        self.assertTrue(ShareForm(document=doc,actor=self.tech).fields['employee'].queryset.filter(pk=self.rep.pk).exists())
        self.assertFalse(ShareForm(document=unrelated,actor=self.tech).fields['employee'].queryset.filter(pk=self.rep.pk).exists())
        self.client.force_login(self.tech);url=reverse('document_share',args=[doc.pk])
        response=self.client.post(url,dict(expected_revision=doc.revision,employee=str(self.rep.pk),permission='view'));self.assertEqual(response.status_code,302)
        self.assertTrue(DocumentAccess.objects.get(document=doc,user=self.rep).enabled)
        s.save_representative(self.leader,order.company_id,rep.pk,rep.version,self.rep,False);doc.refresh_from_db()
        response=self.client.post(url,dict(expected_revision=doc.revision,employee=str(self.rep.pk),permission='closed'));self.assertEqual(response.status_code,302)
        self.assertFalse(DocumentAccess.objects.get(document=doc,user=self.rep).enabled)
