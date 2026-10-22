import secrets
import uuid
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.core import signing
from django.core.exceptions import ValidationError
from django.http import Http404
from django.utils import timezone
from accounts.models import User,AccessGrant,AuditEvent
from documents.models import Person,Document,DocumentAccess
from projects.models import LegalEntity,ProjectObject,Contract,ContractObject,ObjectAccess,ContractAccess,COMPANY_TAX_ID
from repairs.models import Section,Job,Defect,JobDefect,Report,ImportBatch
from repairs.forms import JobForm
from repairs.access import visible_jobs
from .workspace import allocate_legacy

class ContractFoundationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.secret=secrets.token_urlsafe(24)
        cls.leader=User.objects.create_user('+70000000001',cls.secret,full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        cls.tech=User.objects.create_user('+70000000002',cls.secret,full_name='ТЕСТ Техник',role='technician',must_change_password=False)
        cls.worker=User.objects.create_user('+70000000003',cls.secret,full_name='ТЕСТ Мастер',role='foreman',must_change_password=False)
        cls.other=User.objects.create_user('+70000000004',cls.secret,full_name='ТЕСТ Другой',role='foreman',must_change_password=False)
        cls.company=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
        cls.a=ProjectObject.objects.create(legal_entity=cls.company,name='НПС Татарская')
        cls.b=ProjectObject.objects.create(legal_entity=cls.company,name='Скрытая площадка')
        cls.ca=Contract.objects.create(legal_entity=cls.company,number='ТЗС-1726-2025',subject='Устранение дефектов',work_kinds=['repairs'],private_notes='СЕКРЕТНАЯ ПРИБЫЛЬ',amount=Decimal('1234567.89'))
        cls.cb=Contract.objects.create(legal_entity=cls.company,number='ЧУЖОЙ КОНТРАКТ',subject='Другие работы',work_kinds=['repairs'])
        for c in (cls.ca,cls.cb):ContractObject.objects.create(contract=c,project_object=cls.a)
        for u in (cls.tech,cls.worker):
            ObjectAccess.objects.create(user=u,project_object=cls.a,granted_by=cls.leader)
            ContractAccess.objects.create(user=u,contract=cls.ca,show_number=True,show_subject=True,granted_by=cls.leader)
        for key in ['users.manage','projects.manage','projects.access','personnel.manage','repairs.prepare']:
            AccessGrant.objects.create(user=cls.tech,permission=key,granted_by=cls.leader)
        cls.section=Section.objects.create(project_object=cls.a,rnu='Омское РНУ',pipeline='Омск–Татарск',name='Участок')
        cls.hidden_section=Section.objects.create(project_object=cls.b,rnu='Скрыто',pipeline='Скрыто',name='Скрыто')
        cls.legacy=Job.objects.create(section=cls.section,method='p1',length_m=1,assigned_to=cls.worker,created_by=cls.leader)
        cls.d=Defect.objects.create(section=cls.section,kilometre='153.5465',number='0001',pipe_section='0123')
        JobDefect.objects.create(job=cls.legacy,defect=cls.d)
        cls.person=Person.objects.create(legal_entity=cls.company,full_name='Зарипов Рафаил Галиллаевич',position='Мастер',created_by=cls.leader)
    def login(self,user=None):self.client.force_login(user or self.tech)
    def job(self,c,number='0002'):
        job=Job.objects.create(contract=c,section=self.section,method='p2',length_m=1,assigned_to=self.worker,created_by=self.leader)
        d=Defect.objects.create(contract=c,section=self.section,kilometre=154,number=number)
        JobDefect.objects.create(job=job,defect=d)
        return job
    def data(self,**kw):return {'contract':self.ca.pk,'section':self.section.pk,'expected_version':0,'kilometre':'154.1235','defect_numbers':'0008','pipe_section':'001','method':'p2','length_m':'1','wall_mm':'8','materials':'unknown',**kw}
    def test_new_job_requires_explicit_contract(self):
        form=JobForm(data=self.data(contract=''),user=self.tech)
        self.assertFalse(form.is_valid());self.assertIn('contract',form.errors)
    def test_job_creation_assigns_contract_to_job_and_defect(self):
        self.login();r=self.client.post(reverse('repair_new'),self.data())
        self.assertEqual(r.status_code,302)
        j=Job.objects.get(defects__number='0008');self.assertEqual(j.contract,self.ca);self.assertEqual(j.defects.get().contract,self.ca);self.assertEqual(j.defects.get().km,'154.124')
    def test_job_cannot_use_another_contract_or_place(self):
        self.login()
        for changes in ({'contract':self.cb.pk},{'section':self.hidden_section.pk}):
            f=JobForm(data=self.data(**changes),user=self.tech);self.assertFalse(f.is_valid())
    def test_job_cannot_use_nonrepair_contract(self):
        self.ca.work_kinds=['valves'];self.ca.save()
        self.assertFalse(JobForm(data=self.data(),user=self.tech).is_valid())
    def test_same_point_can_exist_in_two_contracts_without_sharing_defect_record(self):
        a=self.job(self.ca);b=self.job(self.cb)
        self.assertNotEqual(a.defects.get().pk,b.defects.get().pk)
    def test_contract_access_gates_list_detail_export_report_and_source(self):
        a=self.job(self.ca);b=self.job(self.cb)
        self.login(self.worker)
        self.assertEqual(set(visible_jobs(self.worker).values_list('pk',flat=True)),{a.pk,self.legacy.pk})
        self.assertEqual(self.client.get(reverse('repair_detail',args=[b.pk])).status_code,404)
        self.assertEqual(self.client.get(reverse('repair_report',args=[b.pk,'repaired'])).status_code,404)
        response=self.client.get(reverse('repairs'),{'contract':self.cb.pk});self.assertEqual(response.context['page'].paginator.count,0)
        ContractAccess.objects.filter(user=self.worker,contract=self.ca).delete()
        self.assertEqual(self.client.get(reverse('repair_detail',args=[a.pk])).status_code,404)
        self.assertEqual(self.client.get(reverse('repair_detail',args=[self.legacy.pk])).status_code,200)
    def test_workspace_hides_other_contract_and_does_not_grant_files(self):
        self.login();r=self.client.get(reverse('work_home'));self.assertContains(r,self.ca.number);self.assertNotContains(r,self.cb.number)
        self.assertEqual(self.client.get(reverse('work_contract',args=[self.cb.pk])).status_code,404)
        self.assertEqual(self.client.get(reverse('work_place',args=[self.ca.pk,self.b.pk])).status_code,404)
        doc=Document.objects.create(legal_entity=self.company,title='Личный документ',kind='personal',section='personnel',created_by=self.leader)
        doc.contracts_linked.add(self.ca)
        self.assertEqual(self.client.get(reverse('document_detail',args=[doc.pk])).status_code,404)
    def test_workspace_uses_allowlisted_contract_fields(self):
        rule=ContractAccess.objects.get(user=self.tech,contract=self.ca);rule.show_number=False;rule.save()
        self.login();r=self.client.get(reverse('work_home'))
        self.assertNotContains(r,self.ca.number);self.assertNotContains(r,'СЕКРЕТНАЯ ПРИБЫЛЬ');self.assertNotContains(r,'1234567')
    def test_legacy_is_not_assigned_by_migration_or_view(self):
        self.login();self.client.get(reverse('work_home'));self.legacy.refresh_from_db();self.d.refresh_from_db()
        self.assertIsNone(self.legacy.contract_id);self.assertIsNone(self.d.contract_id)
    def test_explicit_legacy_allocation_preserves_history_and_acceptance(self):
        self.legacy.state='confirmed';self.legacy.accepted_date=timezone.localdate();self.legacy.save()
        report=Report.objects.create(job=self.legacy,author=self.worker,kind='complete',performed_date=timezone.localdate(),outcome='complete',latitude=55,longitude=73)
        before=(self.legacy.pk,self.d.pk,report.pk,self.legacy.accepted_date,Decimal(self.d.kilometre))
        allocate_legacy(self.tech,self.ca,[self.legacy.pk],{str(self.legacy.pk):self.legacy.version})
        self.legacy.refresh_from_db();self.d.refresh_from_db();report.refresh_from_db()
        self.assertEqual((self.legacy.pk,self.d.pk,report.pk,self.legacy.accepted_date,self.d.kilometre),before)
        self.assertEqual(self.legacy.contract,self.ca);self.assertEqual(self.d.contract,self.ca);self.assertEqual(report.author,self.worker)
        self.assertTrue(AuditEvent.objects.filter(details__kind='contract_allocation').exists())
    def test_allocation_rejects_stale_or_unrelated_contract(self):
        with self.assertRaises(ValidationError):allocate_legacy(self.tech,self.ca,[self.legacy.pk],{str(self.legacy.pk):0})
        ContractObject.objects.filter(contract=self.ca).delete()
        with self.assertRaises(Http404):allocate_legacy(self.tech,self.ca,[self.legacy.pk],{str(self.legacy.pk):1})
        self.legacy.refresh_from_db();self.assertIsNone(self.legacy.contract_id)
    def test_cannot_split_shared_legacy_defect_history(self):
        j=Job.objects.create(section=self.section,method='p1',length_m=1,created_by=self.leader);JobDefect.objects.create(job=j,defect=self.d)
        with self.assertRaisesMessage(ValidationError,'все работы'):allocate_legacy(self.tech,self.ca,[self.legacy.pk],{str(self.legacy.pk):1})
    def test_cannot_silently_merge_matching_legacy_and_contract_defect(self):
        Defect.objects.create(contract=self.ca,section=self.section,kilometre='153.547',number='0001')
        with self.assertRaisesMessage(ValidationError,'проверить совпадение'):allocate_legacy(self.tech,self.ca,[self.legacy.pk],{str(self.legacy.pk):1})
    def test_link_account_requires_both_grants(self):
        self.login(self.worker);self.assertEqual(self.client.get(reverse('person_account',args=[self.person.pk])).status_code,403)
        AccessGrant.objects.filter(user=self.tech,permission='personnel.manage').delete();self.login();self.assertEqual(self.client.get(reverse('person_account',args=[self.person.pk])).status_code,403)
    def test_create_account_from_person_without_implicit_access(self):
        self.login();r=self.client.post(reverse('person_account',args=[self.person.pk]),{'revision':1,'mode':'create','phone':'+79990000001','role':'foreman'})
        self.assertEqual(r.status_code,200);self.assertContains(r,'Временный доступ создан')
        self.person.refresh_from_db();u=self.person.user;self.assertEqual(u.full_name,self.person.full_name)
        self.assertFalse(u.object_assignments.exists());self.assertFalse(u.contract_access.exists());self.assertFalse(u.document_access.exists());self.assertFalse(u.grants.exists())
    def test_link_existing_user_no_name_based_duplicate_or_overwrite(self):
        self.login();r=self.client.post(reverse('person_account',args=[self.person.pk]),{'revision':1,'mode':'link','account':self.worker.pk})
        self.assertEqual(r.status_code,302);self.person.refresh_from_db();self.assertEqual(self.person.user,self.worker);self.assertEqual(User.objects.count(),4)
        r=self.client.post(reverse('person_account',args=[self.person.pk]),{'revision':1,'mode':'link','account':self.other.pk})
        self.assertEqual(r.status_code,200);self.person.refresh_from_db();self.assertEqual(self.person.user,self.worker)
    def test_link_rejects_leaders_and_previously_linked_user(self):
        self.login()
        for account in (self.leader,):
            r=self.client.post(reverse('person_account',args=[self.person.pk]),{'revision':1,'mode':'link','account':account.pk});self.assertEqual(r.status_code,200);self.person.refresh_from_db();self.assertIsNone(self.person.user_id)
        Person.objects.create(legal_entity=self.company,full_name='Другой',user=self.worker,created_by=self.leader)
        r=self.client.post(reverse('person_account',args=[self.person.pk]),{'revision':1,'mode':'link','account':self.worker.pk});self.assertEqual(r.status_code,200);self.person.refresh_from_db();self.assertIsNone(self.person.user_id)
    def test_delegate_creates_objects_and_contracts_with_explicit_metadata_access(self):
        self.login();r=self.client.post(reverse('object_new'),{'name':'Новая площадка','kind':'НПС','status':'planned'});self.assertEqual(r.status_code,302)
        obj=ProjectObject.objects.get(name='Новая площадка');self.assertTrue(ObjectAccess.objects.filter(user=self.tech,project_object=obj).exists())
        r=self.client.post(reverse('contract_new'),{'number':'НОВЫЙ-2026','subject':'Смешанные работы','status':'planned','work_kinds':['repairs','valves'],'linked_objects':[obj.pk]});self.assertEqual(r.status_code,302)
        c=Contract.objects.get(number='НОВЫЙ-2026');self.assertEqual(c.work_kinds,['repairs','valves']);self.assertTrue(ContractAccess.objects.filter(user=self.tech,contract=c).exists())
    def test_delegate_cannot_link_invisible_object_or_edit_sensitive_fields(self):
        self.login();r=self.client.post(reverse('contract_edit',args=[self.ca.pk]),{'expected_version':1,'number':self.ca.number,'subject':self.ca.subject,'work_kinds':['repairs'],'linked_objects':[self.a.pk],'status':'active','amount':'1','private_notes':'подмена','counterparty':'подмена'})
        self.assertEqual(r.status_code,302);self.ca.refresh_from_db();self.assertEqual(self.ca.amount,Decimal('1234567.89'));self.assertEqual(self.ca.private_notes,'СЕКРЕТНАЯ ПРИБЫЛЬ');self.assertEqual(self.ca.counterparty,'')
        r=self.client.get(reverse('contract_edit',args=[self.ca.pk]));self.assertNotContains(r,'СЕКРЕТНАЯ ПРИБЫЛЬ')
    def test_delegate_cannot_grant_hidden_contract_fields(self):
        self.login();ObjectAccess.objects.create(user=self.other,project_object=self.a,granted_by=self.leader)
        r=self.client.post(reverse('contract_share',args=[self.ca.pk]),{'expected_version':1,'employee':self.other.pk,'enabled':'on','fields':['show_counterparty']})
        self.assertEqual(r.status_code,200);self.assertFalse(ContractAccess.objects.filter(user=self.other,contract=self.ca).exists())
    def test_no_user_administration_by_job_title_alone(self):
        AccessGrant.objects.filter(user=self.tech,permission='users.manage').delete();self.login()
        self.assertEqual(self.client.get(reverse('users')).status_code,403)
    def test_delegate_cannot_grant_power_they_do_not_hold(self):
        self.login();r=self.client.post(reverse('employee_new'),{'full_name':'Новый','phone':'+79990000002','role':'foreman','is_active':'on','permissions':['finance.write']})
        self.assertEqual(r.status_code,200);self.assertFalse(User.objects.filter(phone='+79990000002').exists())
    def test_delegate_cannot_reset_more_privileged_account(self):
        AccessGrant.objects.create(user=self.other,permission='finance.write',granted_by=self.leader);self.login()
        self.assertEqual(self.client.post(reverse('employee_reset',args=[self.other.pk])).status_code,403)
        self.assertEqual(self.client.get(reverse('employee_edit',args=[self.other.pk])).status_code,403)
    def test_created_login_also_has_person_card(self):
        self.login();r=self.client.post(reverse('employee_new'),{'full_name':'ТЕСТ Новый мастер','phone':'+79990000002','role':'foreman','is_active':'on','permissions':[]})
        self.assertEqual(r.status_code,200);u=User.objects.get(phone='+79990000002');self.assertEqual(u.person_card.full_name,'ТЕСТ Новый мастер')
    def test_mixed_and_nonrepair_interfaces(self):
        self.ca.work_kinds=['repairs','valves'];self.ca.save();self.login();r=self.client.get(reverse('work_place',args=[self.ca.pk,self.a.pk]));self.assertContains(r,'Устранение дефектов');self.assertContains(r,'Замена запорной арматуры')
        self.ca.work_kinds=['diagnostics'];self.ca.save();r=self.client.get(reverse('work_place',args=[self.ca.pk,self.a.pk]));self.assertNotContains(r,'Загрузить ведомость Excel');self.assertContains(r,'Подготовка к диагностике')
    def test_cannot_remove_place_or_repair_kind_with_bound_jobs(self):
        self.job(self.ca);self.login(self.leader)
        for changes in ({'work_kinds':['valves'],'linked_objects':[self.a.pk]},{'work_kinds':['repairs'],'linked_objects':[]}):
            r=self.client.post(reverse('contract_edit',args=[self.ca.pk]),{'expected_version':1,'number':self.ca.number,'subject':self.ca.subject,'status':'active',**changes});self.assertEqual(r.status_code,200)
            self.ca.refresh_from_db();self.assertEqual(self.ca.work_kinds,['repairs']);self.assertTrue(self.ca.objects_linked.filter(pk=self.a.pk).exists())
    def test_import_requires_contract_and_rejects_unrelated_scope(self):
        from repairs.import_views import UploadForm,matches
        a=self.job(self.ca);b=self.job(self.cb)
        item={'defects':[{'number':'0002','km':'154'}]}
        self.assertEqual(list(matches(self.section,item,self.ca)),[a]);self.assertEqual(list(matches(self.section,item,self.cb)),[b])
        f=UploadForm(user=self.tech,data={'section':self.section.pk});self.assertFalse(f.is_valid());self.assertIn('contract',f.errors)
    def test_assignees_require_object_and_contract_access(self):
        from repairs.views import assignees
        self.assertIn(self.worker,assignees(self.section,self.ca));ContractAccess.objects.filter(user=self.worker,contract=self.ca).delete();self.assertNotIn(self.worker,assignees(self.section,self.ca));self.assertIn(self.leader,assignees(self.section,self.ca))

    def test_xlsx_export_carries_contract_and_rejects_accidental_cross_contract_import(self):
        from repairs.excel import workbook
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.job(self.ca);self.job(self.cb)
        self.login(self.leader)
        exported=self.client.get(reverse('repair_excel_export'),{'contract':self.ca.pk})
        from repairs.importer import parse_work
        parsed=parse_work(SimpleUploadedFile('export.xlsx',exported.content))
        self.assertTrue(all(j['contract_id']==str(self.ca.pk) for j in parsed['jobs']))
        response=self.client.post(reverse('repair_import'),{'contract':self.cb.pk,'section':self.section.pk,'file':SimpleUploadedFile('export.xlsx',exported.content)})
        self.assertContains(response,'другого контракта');self.assertFalse(ImportBatch.objects.exists())
