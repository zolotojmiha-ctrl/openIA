import io,secrets,uuid,tempfile
from datetime import timedelta
from django.test import TestCase,override_settings
from django.urls import reverse
from django.core.exceptions import ValidationError,PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from django.db import transaction
from accounts.models import User,AccessGrant,AuditEvent
from documents.models import Document,DocumentAccess
from .models import LegalEntity,ProjectObject,ObjectAccess,Contract,ContractObject,ContractAccess,COMPANY_TAX_ID,Equipment,EquipmentAssignment,EquipmentDocument,EquipmentImportReceipt
from . import equipment as service,equipment_excel as excel
from repairs.excel import workbook

class EquipmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        password=secrets.token_urlsafe(24)
        cls.leader=User.objects.create_user('+70000000001',password,leader_slot=1,full_name='ТЕСТ Руководитель',must_change_password=False)
        cls.mechanic=User.objects.create_user('+70000000002',password,role='mechanic',full_name='ТЕСТ Механик',must_change_password=False)
        cls.worker=User.objects.create_user('+70000000003',password,role='foreman',full_name='ТЕСТ Мастер',must_change_password=False)
        cls.company=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
        cls.a=ProjectObject.objects.create(legal_entity=cls.company,name='ТЕСТ Омск–Татарск');cls.b=ProjectObject.objects.create(legal_entity=cls.company,name='ТЕСТ НПС')
        cls.contract=Contract.objects.create(legal_entity=cls.company,number='СЕКРЕТНЫЙ-1726',subject='Не раскрывать',work_kinds=['repairs'])
        ContractObject.objects.create(contract=cls.contract,project_object=cls.a)
        for u in (cls.mechanic,cls.worker):ObjectAccess.objects.create(user=u,project_object=cls.a,granted_by=cls.leader)
        ContractAccess.objects.create(user=cls.mechanic,contract=cls.contract,granted_by=cls.leader)
        cls.today=timezone.localdate()
    def grant(self):AccessGrant.objects.get_or_create(user=self.mechanic,permission='equipment.manage',defaults={'granted_by':self.leader})
    def data(self,**overrides):return dict(name='ТЕСТ Экскаватор',category='Экскаватор',inventory_number='0001',registration_number='А001АА',serial_number='VIN-001',ownership='own',owner='',status='ready',notes='') | overrides
    def make(self,actor=None,**overrides):return service.save_equipment(actor or self.leader,None,None,self.data(**overrides))
    def assignment(self,item=None,obj=None,start=None,end=None):
        item=item or self.make();item.refresh_from_db();return service.assign(self.leader,item.pk,item.version,obj or self.a,None,start or self.today,end)
    def doc(self):return Document.objects.create(legal_entity=self.company,title='ТЕСТ Страховка',section='general',created_by=self.leader)
    def file_data(self,doc,**changes):return {'document':doc,'label':'Страховка','expires_on':self.today+timedelta(days=10),'remind':True,'days_before':30,'active':True,**changes}
    def test_mechanic_title_does_not_grant_management(self):
        with self.assertRaises(PermissionDenied):self.make(self.mechanic)
        self.client.force_login(self.mechanic);self.assertEqual(self.client.get(reverse('equipment_new')).status_code,403)
        self.grant();self.assertEqual(self.make(self.mechanic).created_by,self.mechanic)
    def test_register_unique_inventory_preserves_zeroes_and_version(self):
        item=self.make();self.assertEqual(item.inventory_number,'0001')
        with self.assertRaises(ValidationError):self.make()
        service.save_equipment(self.leader,item.pk,1,{**self.data(),'name':'Новая модель'})
        with self.assertRaises(ValidationError):service.save_equipment(self.leader,item.pk,1,self.data())
        item.refresh_from_db();self.assertEqual(item.version,2);self.assertEqual(item.name,'Новая модель');self.assertEqual(AuditEvent.objects.filter(action='equipment_updated').count(),2)
    def test_own_rent_subcontractor_choices(self):
        for n,kind in enumerate(('own','rent','subcontractor')):self.make(inventory_number=str(n),ownership=kind)
        with self.assertRaises(ValidationError):self.make(inventory_number='4',ownership='invalid')
    def test_view_scope_and_no_implicit_object_or_file_access(self):
        item=self.make();doc=self.doc();self.assertFalse(service.visible_equipment(self.worker).exists())
        self.assignment(item)
        self.assertTrue(service.visible_equipment(self.worker).filter(pk=item.pk).exists())
        self.assertFalse(DocumentAccess.objects.filter(user=self.worker).exists())
        self.assertEqual(ObjectAccess.objects.filter(user=self.worker).count(),1)
        self.client.force_login(self.worker);self.assertEqual(self.client.get(reverse('equipment_detail',args=[item.pk])).status_code,200);self.assertEqual(self.client.get(reverse('equipment_edit',args=[item.pk])).status_code,403)
    def test_history_hides_other_objects_and_contract_fields(self):
        item=self.make();self.assignment(item,start=self.today-timedelta(days=10),end=self.today-timedelta(days=1));self.assignment(item,obj=self.b)
        self.client.force_login(self.worker);r=self.client.get(reverse('equipment_detail',args=[item.pk]));self.assertNotContains(r,'ТЕСТ НПС')
        item2=self.make(inventory_number='0002');service.assign(self.leader,item2.pk,item2.version,self.a,self.contract,self.today)
        self.grant();self.client.force_login(self.mechanic);r=self.client.get(reverse('equipment_detail',args=[item2.pk]));self.assertNotContains(r,'СЕКРЕТНЫЙ-1726');self.assertContains(r,'ограниченный доступ')
    def test_subcontractor_does_not_receive_whole_object_fleet(self):
        self.worker.role='subcontractor';self.worker.save();item=self.make();self.assignment(item)
        self.assertFalse(service.visible_equipment(self.worker).exists())
    def test_overlap_and_future_dates_are_rejected_atomically(self):
        item=self.make();a=self.assignment(item,start=self.today-timedelta(days=10));item.refresh_from_db()
        with self.assertRaises(ValidationError):service.assign(self.leader,item.pk,item.version,self.b,None,self.today)
        self.assertEqual(EquipmentAssignment.objects.count(),1);a.refresh_from_db();self.assertIsNone(a.end_date)
        with self.assertRaises(ValidationError):service.assign(self.leader,item.pk,item.version,self.b,None,self.today,self.today-timedelta(days=1))
    def test_transfer_closes_previous_period_and_keeps_history(self):
        item=self.make();old=self.assignment(item,start=self.today-timedelta(days=10));item.refresh_from_db()
        new=service.assign(self.leader,item.pk,item.version,self.b,None,self.today,transfer=True)
        old.refresh_from_db();self.assertEqual(old.end_date,self.today-timedelta(days=1));self.assertEqual(old.version,2);self.assertEqual(new.project_object,self.b);self.assertEqual(service.current(service.placements(self.leader)).get().pk,new.pk)
    def test_transfer_failure_does_not_close_previous_period(self):
        item=self.make();old=self.assignment(item,start=self.today);item.refresh_from_db()
        with self.assertRaises(ValidationError):service.assign(self.leader,item.pk,item.version,self.b,None,self.today,transfer=True)
        old.refresh_from_db();self.assertIsNone(old.end_date)
    def test_assignment_requires_object_and_contract_rights(self):
        self.grant();item=self.make()
        with self.assertRaises(ValidationError):service.assign(self.mechanic,item.pk,item.version,self.b,None,self.today)
        with self.assertRaises(ValidationError):service.assign(self.leader,item.pk,item.version,self.b,self.contract,self.today)
        ContractAccess.objects.filter(user=self.mechanic).delete()
        with self.assertRaises(ValidationError):service.assign(self.mechanic,item.pk,item.version,self.a,self.contract,self.today)
    def test_close_assignment_and_archive_restore(self):
        item=self.make();row=self.assignment(item,start=self.today-timedelta(days=2));item.refresh_from_db()
        with self.assertRaises(ValidationError):service.save_equipment(self.leader,item.pk,item.version,{**self.data(),'status':'archived'})
        service.end_assignment(self.leader,row.pk,row.version,self.today-timedelta(days=1));item.refresh_from_db();service.save_equipment(self.leader,item.pk,item.version,{**self.data(),'status':'archived'});item.refresh_from_db()
        with self.assertRaises(ValidationError):service.assign(self.leader,item.pk,item.version,self.a,None,self.today)
        service.save_equipment(self.leader,item.pk,item.version,self.data());self.assertEqual(item.placements.count(),1)
    def test_document_links_do_not_grant_file_access(self):
        item=self.make();self.assignment(item);doc=self.doc();row=service.save_file(self.leader,item.pk,None,None,self.file_data(doc))
        self.assertFalse(service.equipment_files(self.worker).exists());self.assertFalse(service.due_documents(self.worker))
        DocumentAccess.objects.create(document=doc,user=self.worker,enabled=True,granted_by=self.leader)
        self.assertEqual(service.equipment_files(self.worker).get().pk,row.pk);self.assertEqual(len(service.due_documents(self.worker)),1)
    def test_opt_in_expiration_and_hide_restore(self):
        item=self.make();doc=self.doc();row=service.save_file(self.leader,item.pk,None,None,self.file_data(doc,remind=False));self.assertEqual(service.due_documents(self.leader),[])
        service.save_file(self.leader,item.pk,row.pk,row.version,self.file_data(doc));row.refresh_from_db();self.assertEqual(len(service.due_documents(self.leader)),1)
        service.save_file(self.leader,item.pk,row.pk,row.version,self.file_data(doc,active=False));row.refresh_from_db();self.assertFalse(service.due_documents(self.leader))
        service.save_file(self.leader,item.pk,row.pk,row.version,self.file_data(doc));self.assertEqual(len(service.due_documents(self.leader)),1)
        with self.assertRaises(ValidationError):service.save_file(self.leader,item.pk,row.pk,1,self.file_data(doc))
    def test_reminder_validation_and_dates(self):
        item=self.make();doc=self.doc()
        for changes in ({'expires_on':None},{'days_before':366},{'days_before':-1}):
            with self.assertRaises(ValidationError):service.save_file(self.leader,item.pk,None,None,self.file_data(doc,**changes))
        service.save_file(self.leader,item.pk,None,None,self.file_data(doc,expires_on=self.today+timedelta(days=31)));self.assertFalse(service.due_documents(self.leader))
    def test_mechanic_cannot_link_hidden_document(self):
        self.grant();item=self.make();doc=self.doc()
        with self.assertRaises(ValidationError):service.save_file(self.mechanic,item.pk,None,None,self.file_data(doc))
    def test_current_object_tab_and_mechanic_dashboard(self):
        item=self.make();self.assignment(item);doc=self.doc();service.save_file(self.leader,item.pk,None,None,self.file_data(doc))
        self.client.force_login(self.leader);r=self.client.get(reverse('object_detail',args=[self.a.pk]));self.assertContains(r,'Техника на участке сейчас');self.assertContains(r,'ТЕСТ Экскаватор');self.assertContains(self.client.get(reverse('dashboard')),'Проверить сроки документов: 1')
    def book(self,kind,rows):return SimpleUploadedFile('equipment.xlsx',workbook(rows,headers=excel.HEADERS[kind]),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    def test_xlsx_preview_read_only_and_apply_retry_safe(self):
        self.client.force_login(self.leader);url=reverse('equipment_import',args=['register'])
        row=['','ТЕСТ Кран','Кран','0007','','','own','','ready','','']
        r=self.client.post(url,{'file':self.book('register',[row])});self.assertEqual(r.status_code,200);self.assertFalse(Equipment.objects.exists());self.assertFalse(AuditEvent.objects.filter(action='equipment_updated').exists())
        token=r.context['preview'];self.assertTrue(token)
        for _ in range(2):self.assertEqual(self.client.post(url,{'apply':'1','preview':token}).status_code,302)
        self.assertEqual(Equipment.objects.count(),1);self.assertEqual(EquipmentImportReceipt.objects.count(),1)
    def test_xlsx_update_round_trip_and_stale_version(self):
        item=self.make();row=excel.export_rows(self.leader,'register')[0];row[1]='Исправленная модель'
        rows=excel.parse_rows(self.book('register',[row]),'register',self.leader);item.refresh_from_db();self.assertEqual(item.name,'ТЕСТ Экскаватор')
        with transaction.atomic():excel.apply_rows(self.leader,'register',rows)
        item.refresh_from_db();self.assertEqual(item.name,'Исправленная модель')
        with self.assertRaises(ValidationError):excel.parse_rows(self.book('register',[row]),'register',self.leader)
    def test_xlsx_later_bad_row_rolls_back_entire_import(self):
        rows=[{'row':2,'values':['','ТЕСТ','Кран','0007','','','own','','ready','','']},{'row':3,'values':['','ТЕСТ','Кран','0008','','','bad','','ready','','']}]
        with self.assertRaises(ValidationError):
            with transaction.atomic():excel.apply_rows(self.leader,'register',rows)
        self.assertFalse(Equipment.objects.exists())
    def test_xlsx_assignments_and_documents_round_trip(self):
        item=self.make();self.assignment(item);doc=self.doc();service.save_file(self.leader,item.pk,None,None,self.file_data(doc))
        for kind in ['assignments','documents']:
            rows=excel.export_rows(self.leader,kind);parsed=excel.parse_rows(self.book(kind,rows),kind,self.leader)
            with transaction.atomic():excel.apply_rows(self.leader,kind,parsed)
        self.assertEqual(EquipmentAssignment.objects.count(),1);self.assertEqual(EquipmentDocument.objects.count(),1)
    def test_xlsx_rejects_forged_preview_and_nonmanager(self):
        self.client.force_login(self.leader);self.assertEqual(self.client.post(reverse('equipment_import',args=['register']),{'apply':'1','preview':'forged'}).status_code,409)
        self.client.force_login(self.worker);self.assertEqual(self.client.get(reverse('equipment_export',args=['register'])).status_code,403)
    def test_xlsx_export_hides_contract_ids_without_grant(self):
        item=self.make();service.assign(self.leader,item.pk,item.version,self.a,self.contract,self.today);self.grant();ContractAccess.objects.filter(user=self.mechanic).delete()
        self.assertEqual(excel.export_rows(self.mechanic,'assignments')[0][3],'')
    def test_assignment_does_not_change_equipment_condition(self):
        item=self.make(status='repair');self.assignment(item);item.refresh_from_db();self.assertEqual(item.status,'repair')

    def test_upload_from_card_links_file_once_and_preserves_file_permissions(self):
        item=self.make();self.client.force_login(self.leader);token=uuid.uuid4();url=reverse('document_upload')+'?equipment='+str(item.pk)
        with tempfile.TemporaryDirectory() as root,override_settings(PRIVATE_DOCUMENT_ROOT=root):
            for _ in range(2):
                r=self.client.post(url,{'file':SimpleUploadedFile('ТЕСТ паспорт.txt','Тест'.encode(),content_type='text/plain'),'section':'general','collision':'ask','defer_link':'on','upload_token':str(token)},HTTP_X_REQUESTED_WITH='XMLHttpRequest')
                self.assertEqual(r.status_code,200,r.content)
            row=EquipmentDocument.objects.get(equipment=item)
            self.assertFalse(row.remind);self.assertEqual(row.document.versions.count(),1);self.assertFalse(DocumentAccess.objects.filter(document=row.document).exists())
            self.assertContains(self.client.get(reverse('document_detail',args=[row.document_id])),reverse('equipment_detail',args=[item.pk]))
        self.client.force_login(self.worker);self.assertEqual(self.client.get(url).status_code,403)
    def test_invalid_assignment_target_does_not_crash(self):
        self.client.force_login(self.leader)
        self.assertEqual(self.client.get(reverse('equipment')+'?assign_object='+'-'*36).status_code,200)
        self.assertEqual(self.client.get(reverse('document_upload')+'?equipment=not-an-id').status_code,404)

    def test_archive_state_filter_finds_archived_machines(self):
        item=self.make(status='archived');self.client.force_login(self.leader)
        response=self.client.get(reverse('equipment')+'?state=archived')
        self.assertContains(response,item.name);self.assertTrue(response.context['archive'])
