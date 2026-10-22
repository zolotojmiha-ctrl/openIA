import secrets,uuid,io
from datetime import timedelta
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import ValidationError,PermissionDenied
from django.utils import timezone
from accounts.models import User,AccessGrant
from documents.models import Person,PersonAssignment,DocumentAccess
from projects.models import LegalEntity,ProjectObject,Contract,ContractObject,ObjectAccess,ContractAccess,Brigade,BrigadeMember,BrigadeAssignment,CrewImportReceipt,COMPANY_TAX_ID
from repairs.models import Job,Section,Defect,JobDefect,Report
from repairs.access import reportable_jobs
from .crews import create_crew,add_member,update_crew,assign_crew_place,close_assignment,close_member,update_membership,assign_jobs_to_crew
from .crew_excel import parse_rows,export_rows,HEADERS
from repairs.excel import workbook
from django.core.files.uploadedfile import SimpleUploadedFile

class CrewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        pwd=secrets.token_urlsafe(24)
        cls.leader=User.objects.create_user('+70000000001',pwd,leader_slot=1,full_name='ТЕСТ Руководитель',must_change_password=False)
        cls.tech=User.objects.create_user('+70000000002',pwd,role='technician',full_name='ТЕСТ Техник',must_change_password=False)
        cls.master=User.objects.create_user('+70000000003',pwd,role='foreman',full_name='ТЕСТ Мастер',must_change_password=False)
        cls.second=User.objects.create_user('+70000000004',pwd,role='foreman',full_name='ТЕСТ Второй',must_change_password=False)
        for key in ['personnel.manage','repairs.prepare']:AccessGrant.objects.create(user=cls.tech,permission=key,granted_by=cls.leader)
        cls.company=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
        cls.obj=ProjectObject.objects.create(legal_entity=cls.company,name='ТЕСТ Площадка')
        cls.obj2=ProjectObject.objects.create(legal_entity=cls.company,name='ТЕСТ Площадка 2')
        cls.contract=Contract.objects.create(legal_entity=cls.company,number='ТЕСТ-К',subject='Ремонты',work_kinds=['repairs'])
        for obj in (cls.obj,cls.obj2):
            ContractObject.objects.create(contract=cls.contract,project_object=obj)
            for u in (cls.tech,cls.master,cls.second):ObjectAccess.objects.create(user=u,project_object=obj,granted_by=cls.leader)
        for u in (cls.tech,cls.master,cls.second):ContractAccess.objects.create(user=u,contract=cls.contract,granted_by=cls.leader,show_number=True)
        cls.person=Person.objects.create(legal_entity=cls.company,full_name='ТЕСТ Зарипов',position='Мастер',user=cls.master,created_by=cls.leader)
        cls.person2=Person.objects.create(legal_entity=cls.company,full_name='ТЕСТ Второй мастер',position='Мастер',user=cls.second,created_by=cls.leader)
        cls.worker=Person.objects.create(legal_entity=cls.company,full_name='ТЕСТ Рабочий без аккаунта',position='Рабочий',created_by=cls.leader)
        cls.today=timezone.localdate();cls.start=cls.today-timedelta(days=10)
        cls.section=Section.objects.create(project_object=cls.obj,rnu='ТЕСТ',pipeline='ТЕСТ',name='ТЕСТ')
    def crew(self,person=None,members=None):return create_crew(self.tech,'ТЕСТ Бригада',person or self.person,members if members is not None else [self.worker],self.start)
    def assignment(self,crew,obj=None):crew.refresh_from_db();return assign_crew_place(self.tech,crew.pk,crew.version,self.contract.pk,(obj or self.obj).pk,self.start,None)
    def job(self):
        j=Job.objects.create(contract=self.contract,section=self.section,method='grind',assigned_to=self.master,created_by=self.leader)
        d=Defect.objects.create(contract=self.contract,section=self.section,kilometre=1,number='0001');JobDefect.objects.create(job=j,defect=d);return j
    def test_create_includes_foreman_and_no_implicit_grants(self):
        crew=self.crew();self.assertEqual(crew.memberships.count(),2);self.assertFalse(User.objects.filter(full_name=self.worker.full_name).exists());self.assertFalse(DocumentAccess.objects.exists())
        with self.assertRaises(PermissionDenied):create_crew(self.master,'Нельзя',self.person,[],self.today)
    def test_membership_periods_preserve_closed_records(self):
        crew=self.crew();m=crew.memberships.get(person=self.worker)
        with self.assertRaises(ValidationError):add_member(self.tech,crew.pk,crew.version,self.worker,self.today,None)
        close_member(self.tech,m.pk,m.version,self.today-timedelta(days=1));m.refresh_from_db();self.assertIsNotNone(m.end_date)
        crew.refresh_from_db();add_member(self.tech,crew.pk,crew.version,self.worker,self.today,None)
        self.assertEqual(crew.memberships.filter(person=self.worker).count(),2)
    def test_assignment_records_entire_roster_and_dates_for_each_person(self):
        crew=self.crew();a=self.assignment(crew)
        self.assertEqual(len(a.roster),2);self.assertEqual(a.person_assignments.count(),2);self.assertEqual(a.master,self.master)
        self.assertTrue(all(p.start_date==self.start for p in a.person_assignments.all()))
        self.worker.full_name='Изменённое имя';self.worker.save();a.refresh_from_db();self.assertEqual(next(p for p in a.roster if p['person']==str(self.worker.pk))['name'],'ТЕСТ Рабочий без аккаунта')
    def test_parallel_assignments_of_same_person_allowed(self):
        crew=self.crew();a=self.assignment(crew);b=self.assignment(crew,self.obj2)
        self.assertEqual(PersonAssignment.objects.filter(person=self.person).count(),2);self.assertNotEqual(a.pk,b.pk)
    def test_missing_master_account_or_grants_rejected_without_partial_assignments(self):
        crew=self.crew();ContractAccess.objects.filter(user=self.master).delete()
        with self.assertRaises(ValidationError):self.assignment(crew)
        self.assertFalse(BrigadeAssignment.objects.exists());self.assertFalse(PersonAssignment.objects.exists())
        self.person.user=None;self.person.save()
        with self.assertRaises(ValidationError):self.assignment(crew)
    def test_assignment_rejects_stale_roster(self):
        crew=self.crew();version=crew.version;add_member(self.tech,crew.pk,version,self.person2,self.today,None)
        with self.assertRaises(ValidationError):assign_crew_place(self.tech,crew.pk,version,self.contract.pk,self.obj.pk,self.today,None)
    def test_assignment_foreman_must_be_in_dated_roster(self):
        crew=self.crew();m=crew.memberships.get(person=self.person);close_member(self.tech,m.pk,m.version,self.today-timedelta(days=1));crew.refresh_from_db()
        with self.assertRaises(ValidationError):assign_crew_place(self.tech,crew.pk,crew.version,self.contract.pk,self.obj.pk,self.today,None)
    def test_close_period_updates_related_person_history_only(self):
        crew=self.crew();a=self.assignment(crew);b=self.assignment(crew,self.obj2)
        close_assignment(self.tech,a.pk,a.version,self.today)
        self.assertTrue(all(p.end_date==self.today for p in a.person_assignments.all()));self.assertTrue(all(p.end_date is None for p in b.person_assignments.all()))
    def test_foreman_change_preserves_old_assignment_master(self):
        crew=self.crew();a=self.assignment(crew);crew.refresh_from_db();add_member(self.tech,crew.pk,crew.version,self.person2,self.today,None);crew.refresh_from_db()
        update_crew(self.tech,crew.pk,crew.version,crew.name,self.person2)
        a.refresh_from_db();self.assertEqual(a.master,self.master)
    def test_closed_assignment_cannot_report_or_receive_new_jobs(self):
        crew=self.crew();a=self.assignment(crew);j=self.job();assign_jobs_to_crew(self.tech,a.pk,{str(j.pk):j.version});close_assignment(self.tech,a.pk,a.version,self.today-timedelta(days=1))
        self.assertFalse(reportable_jobs(self.master).filter(pk=j.pk).exists())
        j.refresh_from_db()
        with self.assertRaises(ValidationError):assign_jobs_to_crew(self.tech,a.pk,{str(j.pk):j.version})
    def test_handover_keeps_previous_result_and_changes_master(self):
        first=self.crew();a=self.assignment(first);j=self.job();original=Report.objects.create(job=j,author=self.master,kind='note',performed_date=self.today,note='Первая бригада')
        assign_jobs_to_crew(self.tech,a.pk,{str(j.pk):j.version});j.refresh_from_db()
        second=self.crew(self.person2,[]);b=self.assignment(second);assign_jobs_to_crew(self.tech,b.pk,{str(j.pk):j.version})
        j.refresh_from_db();original.refresh_from_db();self.assertEqual(j.assigned_to,self.second);self.assertEqual(j.crew_assignment,b);self.assertEqual(original.author,self.master);self.assertEqual(j.reports.filter(kind='assignment').count(),2)
        self.assertFalse(reportable_jobs(self.master).filter(pk=j.pk).exists());self.assertTrue(reportable_jobs(self.second).filter(pk=j.pk).exists())
    def test_handover_wrong_place_or_stale_version_is_atomic(self):
        crew=self.crew();a=self.assignment(crew,self.obj2);j=self.job()
        with self.assertRaises(ValidationError):assign_jobs_to_crew(self.tech,a.pk,{str(j.pk):j.version})
        self.assertFalse(j.reports.exists());j.refresh_from_db();self.assertIsNone(j.crew_assignment_id)
    def test_archive_requires_completed_assignments(self):
        crew=self.crew();self.assignment(crew);crew.refresh_from_db()
        with self.assertRaises(ValidationError):update_crew(self.tech,crew.pk,crew.version,crew.name,self.person,True)
    def xlsx(self,kind,rows):return SimpleUploadedFile('test.xlsx',workbook(rows,headers=HEADERS[kind]))
    def test_xlsx_preview_is_read_only_and_apply_is_idempotent(self):
        self.client.force_login(self.tech)
        values=['','Новая бригада',str(self.person.pk),'справочно',self.today.isoformat(),'0','']
        r=self.client.post(reverse('crew_excel_import',args=['brigades']),{'file':self.xlsx('brigades',[values])})
        self.assertEqual(r.status_code,200);self.assertFalse(Brigade.objects.exists());self.assertTrue(r.context['preview'])
        payload={'apply':'1','preview':r.context['preview']}
        self.assertEqual(self.client.post(reverse('crew_excel_import',args=['brigades']),payload).status_code,302)
        self.assertEqual(self.client.post(reverse('crew_excel_import',args=['brigades']),payload).status_code,302)
        self.assertEqual(Brigade.objects.count(),1);self.assertEqual(CrewImportReceipt.objects.count(),1)
    def test_xlsx_update_uses_version_and_does_not_guess_identity(self):
        crew=self.crew();rows=export_rows(self.tech,'brigades');rows[0][1]='Новое название'
        self.client.force_login(self.tech);r=self.client.post(reverse('crew_excel_import',args=['brigades']),{'file':self.xlsx('brigades',rows)})
        self.assertTrue(r.context['preview']);crew.refresh_from_db();self.assertEqual(crew.name,'ТЕСТ Бригада')
        self.client.post(reverse('crew_excel_import',args=['brigades']),{'apply':'1','preview':r.context['preview']});crew.refresh_from_db();self.assertEqual(crew.name,'Новое название')
        r=self.client.post(reverse('crew_excel_import',args=['brigades']),{'file':self.xlsx('brigades',rows)});self.assertFalse(r.context['preview']);self.assertContains(r,'изменена')
    def test_xlsx_invalid_later_row_rolls_back_entire_file(self):
        first=['','Первая',str(self.person.pk),'',self.today.isoformat(),'0',''];second=['','Ошибка',str(uuid.uuid4()),'',self.today.isoformat(),'0','']
        with self.assertRaises(ValidationError):parse_rows(self.xlsx('brigades',[first,second]),'brigades',self.tech)
        self.assertFalse(Brigade.objects.exists())
    def test_xlsx_members_and_assignments_roundtrip(self):
        crew=self.crew();a=self.assignment(crew)
        for kind in ('members','assignments'):
            rows=export_rows(self.tech,kind);parsed=parse_rows(self.xlsx(kind,rows),kind,self.tech);self.assertEqual(len(parsed),len(rows))
        self.assertEqual(BrigadeAssignment.objects.count(),1)
