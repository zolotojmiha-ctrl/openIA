import tempfile,hashlib,io,zipfile
from datetime import date,timedelta
from django.test import TestCase,override_settings
from django.urls import reverse
from django.utils import timezone
from accounts.models import User,AccessGrant
from documents.models import Person,Document,DocumentVersion,DocumentAccess,PersonAssignment
from documents.storage import private_path
from .models import (LegalEntity,COMPANY_TAX_ID,Contract,ContractObject,ProjectObject,Brigade,Equipment,
 EquipmentCustody,Admission,AdmissionRevision,AdmissionFile,CustomerAccess,CustomerPublication,MaterialRequest)
from repairs.models import Section,Job,Defect,JobDefect

class CompleteWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.leader=User.objects.create_user('+79999981601','123456',leader_slot=1,full_name='Руководитель',must_change_password=False)
        cls.master=User.objects.create_user('+79999981602','123456',role='foreman',full_name='Мастер',must_change_password=False)
        cls.customer=User.objects.create_user('+79999981603','123456',role='customer',full_name='Представитель',must_change_password=False)
        cls.other=User.objects.create_user('+79999981604','123456',role='other',full_name='Другой',must_change_password=False)
        cls.entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
        cls.contract=Contract.objects.create(legal_entity=cls.entity,number='ПУБЛИЧНЫЙ-НОМЕР',subject='Работы',private_notes='СЕКРЕТ-КОНТРАКТА',work_kinds=['repairs'],status='active')
        cls.area=ProjectObject.objects.create(legal_entity=cls.entity,name='Участок 100–300',status='active')
        ContractObject.objects.create(contract=cls.contract,project_object=cls.area)
        cls.person=Person.objects.create(legal_entity=cls.entity,full_name='Мастер без паспорта в публикации',position='Мастер',user=cls.master,created_by=cls.leader)
        cls.worker=Person.objects.create(legal_entity=cls.entity,full_name='Работник без логина',created_by=cls.leader)
        cls.crew=Brigade.objects.create(legal_entity=cls.entity,name='Бригада',foreman=cls.person,created_by=cls.leader)
        cls.equipment=Equipment.objects.create(legal_entity=cls.entity,name='Соболь',category='Авто',inventory_number='616',created_by=cls.leader)
        cls.section=Section.objects.create(project_object=cls.area,rnu='РНУ',pipeline='ОИ',name='Тест')
        cls.job=Job.objects.create(contract=cls.contract,section=cls.section,method='grind',state='confirmed',notes='СЕКРЕТ-ЗАДАНИЯ',created_by=cls.leader)
        cls.defect=Defect.objects.create(contract=cls.contract,section=cls.section,kilometre='123.456',number='001')
        JobDefect.objects.create(job=cls.job,defect=cls.defect)
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.override=override_settings(PRIVATE_DOCUMENT_ROOT=self.tmp.name);self.override.enable();self.addCleanup(self.override.disable)
        self.client.force_login(self.leader)
    def doc(self,title='ППР',content=b'original bytes'):
        import uuid
        key='ab/'+uuid.uuid4().hex;p=private_path(key);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(content)
        doc=Document.objects.create(legal_entity=self.entity,title=title,section='general',created_by=self.leader)
        v=DocumentVersion.objects.create(document=doc,sequence=1,storage_key=key,original_name='proof.txt',content_type='text/plain',size=len(content),sha256=hashlib.sha256(content).hexdigest(),uploaded_by=self.leader)
        doc.current_version=v;doc.save();return doc,v
    def grant(self):return CustomerAccess.objects.create(user=self.customer,contract=self.contract,project_object=self.area,created_by=self.leader)
    def pub_url(self):return reverse('customer_publish',args=[self.contract.pk,self.area.pk])
    def test_customer_isolation_even_if_accidentally_given_internal_file_grant(self):
        doc,v=self.doc('СЕКРЕТ-ПАСПОРТ');DocumentAccess.objects.create(document=doc,user=self.customer,enabled=True,granted_by=self.leader)
        self.client.force_login(self.customer)
        for url in [reverse('people'),reverse('documents'),reverse('finance'),reverse('mail'),reverse('users'),reverse('work_place',args=[self.contract.pk,self.area.pk]),reverse('document_download',args=[doc.pk,v.pk])]:
            self.assertEqual(self.client.get(url).status_code,403,url)
            self.assertEqual(self.client.post(url,{}).status_code,403,url)
        self.assertRedirects(self.client.get(reverse('dashboard')),reverse('customer_portal'))
        from documents.access import visible_documents
        self.assertFalse(visible_documents(self.customer).exists())
    def test_customer_sees_only_published_snapshot_and_revocation_is_immediate(self):
        grant=self.grant();MaterialRequest.objects.create(contract=self.contract,project_object=self.area,name='Муфта П1',quantity=2,unit='шт',created_by=self.leader)
        self.client.force_login(self.customer);r=self.client.get(reverse('customer_portal'));self.assertNotContains(r,'Муфта П1');self.assertNotContains(r,'123.456')
        self.client.force_login(self.leader);r=self.client.post(self.pub_url(),{'expected_version':self.contract.version,'confirm':'on'})
        self.assertEqual(r.status_code,302)
        self.client.force_login(self.customer);r=self.client.get(reverse('customer_portal'));self.assertContains(r,'Муфта П1');self.assertContains(r,'123.456')
        for secret in ['СЕКРЕТ-КОНТРАКТА','СЕКРЕТ-ЗАДАНИЯ',self.person.full_name]:self.assertNotContains(r,secret)
        self.job.notes='ИЗМЕНЕНО';self.job.save();self.assertNotContains(self.client.get(reverse('customer_portal')),'ИЗМЕНЕНО')
        grant.delete();self.assertNotContains(self.client.get(reverse('customer_portal')),'123.456')
    def test_customer_grant_cannot_include_unrelated_area(self):
        area=ProjectObject.objects.create(legal_entity=self.entity,name='Чужой')
        r=self.client.post(reverse('customer_access'),{'user':self.customer.pk,'contract':self.contract.pk,'objects':[area.pk]})
        self.assertEqual(r.status_code,200);self.assertFalse(CustomerAccess.objects.exists())
    def test_equipment_can_be_assigned_without_object_or_contract_and_overlap_rejected(self):
        url=reverse('equipment_custody',args=[self.equipment.pk])
        data={'expected_version':1,'brigade':self.crew.pk,'driver':self.worker.pk,'start_date':'2026-10-27'}
        self.assertEqual(self.client.post(url,data).status_code,302)
        row=EquipmentCustody.objects.get();self.assertEqual(row.driver,self.worker)
        data['expected_version']=2
        self.assertEqual(self.client.post(url,data).status_code,200);self.assertEqual(EquipmentCustody.objects.count(),1)
        end=reverse('custody_end',args=[row.pk]);self.assertEqual(self.client.post(end,{'expected_version':1,'end_date':'2026-10-27'}).status_code,302)
        data['start_date']='2026-10-28';self.assertEqual(self.client.post(url,data).status_code,302);self.assertEqual(EquipmentCustody.objects.count(),2)
    def admission_data(self,doc,**kw):
        return {'expected_version':0,'contract':self.contract.pk,'project_object':self.area.pk,'title':'Устранение дефектов','people':[self.person.pk,self.worker.pk],'equipment':[self.equipment.pk],'status':'submitted','start_date':'2026-10-27','files_ppr':[doc.pk],**kw}
    def test_admission_keeps_original_roster_versions_and_downloads_for_assigned_master(self):
        doc,v=self.doc();url=reverse('admission_new',args=[self.crew.pk])
        r=self.client.post(url,self.admission_data(doc));self.assertEqual(r.status_code,302)
        packet=Admission.objects.get();rev=packet.revisions.get();self.assertEqual(rev.files.get().version,v)
        self.worker.full_name='Новое имя';self.worker.save()
        r=self.client.post(reverse('admission_edit',args=[self.crew.pk,packet.pk]),self.admission_data(doc,expected_version=1,people=[self.person.pk],status='approved',note='Ответ заказчика по почте'))
        self.assertEqual(r.status_code,302);self.assertEqual(packet.revisions.count(),2)
        rev.refresh_from_db();self.assertEqual(len(rev.snapshot['people']),2);self.assertNotEqual(rev.snapshot['people'][1]['name'],'Новое имя')
        self.client.force_login(self.master)
        r=self.client.get(reverse('admission_zip',args=[packet.pk,rev.pk]));self.assertEqual(r.status_code,200)
        content=b''.join(r.streaming_content)
        with zipfile.ZipFile(io.BytesIO(content)) as z:self.assertIn(b'original bytes',[z.read(n) for n in z.namelist()])
        self.client.force_login(self.other);self.assertEqual(self.client.get(reverse('admission_detail',args=[packet.pk])).status_code,404)
    def test_admission_requires_explicit_instruction_confirmation(self):
        doc,v=self.doc();r=self.client.post(reverse('admission_new',args=[self.crew.pk]),self.admission_data(doc,status='instructed'))
        self.assertEqual(r.status_code,200);self.assertFalse(Admission.objects.exists())
    def test_person_document_rename_context_delete_and_no_file_access_after_delete(self):
        doc,v=self.doc();doc.personnel_linked.add(self.worker)
        r=self.client.get(reverse('document_detail',args=[doc.pk])+'?person='+str(self.worker.pk));self.assertContains(r,'К документам сотрудника')
        r=self.client.post(reverse('document_rename',args=[doc.pk])+'?person='+str(self.worker.pk),{'expected_revision':1,'title':'Паспорт'})
        self.assertRedirects(r,reverse('person_detail',args=[self.worker.pk]));doc.refresh_from_db();self.assertEqual(doc.title,'Паспорт')
        with self.captureOnCommitCallbacks(execute=True):r=self.client.post(reverse('document_delete',args=[doc.pk]),{'expected_revision':doc.revision,'confirm':'on'})
        self.assertEqual(r.status_code,302);self.assertFalse(private_path(v.storage_key).exists())
        self.assertEqual(self.client.get(reverse('document_detail',args=[doc.pk])).status_code,404)
    def test_cannot_delete_document_used_in_admission(self):
        doc,v=self.doc();self.client.post(reverse('admission_new',args=[self.crew.pk]),self.admission_data(doc))
        r=self.client.post(reverse('document_delete',args=[doc.pk]),{'expected_revision':doc.revision,'confirm':'on'})
        self.assertEqual(r.status_code,200);doc.refresh_from_db();self.assertIsNone(doc.deleted_at);self.assertTrue(private_path(v.storage_key).exists())
    def test_empty_area_deletion_and_history_protection(self):
        area=ProjectObject.objects.create(legal_entity=self.entity,name='Ошибочный');ContractObject.objects.create(contract=self.contract,project_object=area)
        self.assertEqual(self.client.post(reverse('area_delete',args=[area.pk]),{'expected_version':1,'confirm':'on'}).status_code,302);self.assertFalse(ProjectObject.objects.filter(pk=area.pk).exists())
        r=self.client.post(reverse('area_delete',args=[self.area.pk]),{'expected_version':1,'confirm':'on'});self.assertEqual(r.status_code,200);self.assertTrue(ProjectObject.objects.filter(pk=self.area.pk).exists())
    def test_direct_person_assignment_needs_no_login_and_duplicates_blocked(self):
        data={'person':self.worker.pk,'start_date':'2026-10-27'};url=reverse('area_person',args=[self.contract.pk,self.area.pk])
        self.assertEqual(self.client.post(url,data).status_code,302);self.assertIsNone(self.worker.user_id)
        self.assertEqual(self.client.post(url,data).status_code,200);self.assertEqual(PersonAssignment.objects.filter(person=self.worker).count(),1)
    def test_pto_preset_does_not_grant_finance_or_user_administration(self):
        from accounts.forms import EmployeeForm
        f=EmployeeForm({'full_name':'ПТО','phone':'+79999981609','role':'pto','profile':'pto','is_active':'on'},actor=self.leader)
        self.assertTrue(f.is_valid(),f.errors);self.assertIn('repairs.prepare',f.cleaned_data['permissions']);self.assertNotIn('finance.read',f.cleaned_data['permissions']);self.assertNotIn('users.manage',f.cleaned_data['permissions'])
    def test_deleting_source_keeps_bytes_referenced_by_another_assembled_document(self):
        from documents.models import DocumentPart
        doc,v=self.doc('Исходник');other,other_v=self.doc('Собранный файл',b'assembled')
        DocumentPart.objects.create(version=other_v,position=1,page_count=1,storage_key=v.storage_key,original_name='source.txt',content_type='text/plain',size=v.size,sha256=v.sha256)
        with self.captureOnCommitCallbacks(execute=True):self.client.post(reverse('document_delete',args=[doc.pk]),{'expected_revision':1,'confirm':'on'})
        self.assertTrue(private_path(v.storage_key).exists());self.assertEqual(self.client.get(reverse('document_detail',args=[doc.pk])).status_code,404)
        self.assertEqual(self.client.get(reverse('document_detail',args=[other.pk])).status_code,200)
    def test_defect_identity_stays_frozen_after_confirmed_result_but_description_can_change(self):
        url=reverse('defect_edit',args=[self.defect.pk]);r=self.client.get(url);token=r.context['form'].initial['expected_snapshot']
        r=self.client.post(url,{'expected_snapshot':token,'kilometre':'123.456','number':'001','pipe_section':'','description':'Уточнённое описание'})
        self.assertEqual(r.status_code,302);self.defect.refresh_from_db();self.assertEqual(self.defect.description,'Уточнённое описание')
        token=self.client.get(url).context['form'].initial['expected_snapshot']
        r=self.client.post(url,{'expected_snapshot':token,'kilometre':'124.000','number':'001','pipe_section':'','description':'Описание'})
        self.assertEqual(r.status_code,200);self.defect.refresh_from_db();self.assertEqual(str(self.defect.kilometre),'123.456000')
    def test_publication_withdrawal_hides_history_from_customer(self):
        self.grant();self.client.post(self.pub_url(),{'expected_version':1,'confirm':'on'})
        self.client.post(reverse('customer_withdraw',args=[self.contract.pk,self.area.pk]))
        self.client.force_login(self.customer);r=self.client.get(reverse('customer_portal'))
        self.assertNotContains(r,'123.456');self.assertContains(r,'временно сняты')
