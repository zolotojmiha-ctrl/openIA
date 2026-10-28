from datetime import timedelta
from django.test import TestCase
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.core.exceptions import ValidationError
from django.core import signing
from accounts.models import User,AuditEvent,SupportRequest,BusinessNoticeRead
from accounts.views import audit
from accounts.business_notices import unread
from . import test_crews as fixtures
from .crews import assign_crew_places,close_assignment
from . import equipment as fleet,equipment_excel
from .models import BrigadeAssignment,EquipmentAssignment

class FeedbackTests(TestCase):
    setUpTestData=classmethod(fixtures.CrewTests.setUpTestData.__func__)
    crew=fixtures.CrewTests.crew
    assignment=fixtures.CrewTests.assignment
    job=fixtures.CrewTests.job
    def machine(self):
        return fleet.save_equipment(self.leader,None,None,dict(name='Соболь',category='Автомобиль',inventory_number='1',registration_number='',serial_number='',ownership='own',owner='',status='ready',notes=''))
    def test_bulk_assignment_replacement_preserves_old_rosters_and_other_place(self):
        old=self.crew();a=self.assignment(old);b=self.assignment(old,self.obj2)
        new=self.crew(self.person2)
        result=assign_crew_places(self.tech,new.pk,new.version,self.contract.pk,[self.obj.pk],self.today,None,{str(a.pk):a.version})
        a.refresh_from_db();b.refresh_from_db()
        self.assertEqual(a.end_date,self.today-timedelta(days=1));self.assertIsNone(b.end_date)
        self.assertEqual(result[0].foreman_person,self.person2)
        self.assertEqual(a.roster[0]['name'],'ТЕСТ Зарипов')
        with self.assertRaises(ValidationError):assign_crew_places(self.tech,new.pk,new.version,self.contract.pk,[self.obj.pk],self.today,None,{str(a.pk):1})
        self.assertEqual(BrigadeAssignment.objects.count(),3)
    def test_bulk_late_error_rolls_back_every_place(self):
        crew=self.crew();self.assignment(crew,self.obj2)
        with self.assertRaises(ValidationError):assign_crew_places(self.tech,crew.pk,crew.version,self.contract.pk,[self.obj.pk,self.obj2.pk],self.start,None)
        self.assertEqual(BrigadeAssignment.objects.count(),1)
    def test_crew_card_bulk_form_and_equipment_selection(self):
        crew=self.crew();self.client.force_login(self.leader)
        response=self.client.get(reverse('brigade_assign',args=[crew.pk]),{'contract':self.contract.pk})
        self.assertEqual(response.status_code,200);self.assertContains(response,'Выделить все объекты')
        form=response.context['form']
        response=self.client.post(reverse('brigade_assign',args=[crew.pk])+'?contract='+str(self.contract.pk),{'snapshot':form.initial['snapshot'],'project_objects':[self.obj.pk,self.obj2.pk],'start':self.start.isoformat()})
        self.assertEqual(response.status_code,302);self.assertEqual(BrigadeAssignment.objects.count(),2)
        assignment=BrigadeAssignment.objects.filter(project_object=self.obj).get();item=self.machine()
        response=self.client.get(reverse('equipment_assign',args=[item.pk]),{'crew':assignment.pk})
        self.assertEqual(response.context['form'].initial['crew_assignment'],assignment.pk)
        self.assertEqual(response.context['form'].initial['contract'],self.contract.pk)
    def test_equipment_crew_scope_dates_export_and_close_guard(self):
        crew=self.crew();a=self.assignment(crew);item=self.machine()
        with self.assertRaises(ValidationError):fleet.assign(self.leader,item.pk,1,self.obj2,self.contract,self.today,crew_assignment=a)
        row=fleet.assign(self.leader,item.pk,1,self.obj,self.contract,self.today,crew_assignment=a)
        self.assertEqual(row.crew_assignment,a)
        exported=equipment_excel.export_rows(self.leader,'assignments');self.assertEqual(exported[0][8],str(a.pk))
        with self.assertRaises(ValidationError):close_assignment(self.tech,a.pk,a.version,self.today)
        fleet.end_assignment(self.leader,row.pk,row.version,self.today)
        close_assignment(self.tech,a.pk,a.version,self.today)
        a.refresh_from_db();self.assertEqual(a.end_date,self.today)
    def test_equipment_label_is_editable_and_back_link_exists(self):
        self.client.force_login(self.leader)
        self.assertContains(self.client.get(reverse('repairs')),'← Выполнение работ')
        item=self.machine();r=self.client.get(reverse('equipment_document_new',args=[item.pk]))
        self.assertContains(r,'Название документа на карточке техники')
    def test_both_leaders_receive_business_notices_independently(self):
        other=User.objects.create_user('+70000000005',None,leader_slot=2,full_name='Второй руководитель',must_change_password=False)
        self.client.force_login(self.master)
        self.client.post(reverse('support'),{'text':'Ошибка назначения'})
        ticket=SupportRequest.objects.get();event=AuditEvent.objects.filter(action='support_updated').get()
        self.assertTrue(unread(self.leader).filter(pk=event.pk).exists());self.assertTrue(unread(other).filter(pk=event.pk).exists())
        self.assertFalse(unread(self.master).exists())
        self.client.force_login(self.leader)
        self.assertContains(self.client.get(reverse('dashboard')),'data-business-count')
        response=self.client.get(reverse('notifications'));self.assertGreater(response.json()['business'],0)
        self.client.post(reverse('business_inbox'),{'events':[event.pk]})
        self.assertFalse(unread(self.leader).filter(pk=event.pk).exists());self.assertTrue(unread(other).filter(pk=event.pk).exists())
        self.client.post(reverse('support_detail',args=[ticket.pk]),{'status':'progress','response':'Исправляем'})
        self.assertEqual(unread(self.master).count(),1)
        self.client.force_login(self.master)
        response=self.client.get(reverse('business_inbox'));self.assertContains(response,'Обращение №')
        self.assertNotContains(response,'Изменён доступ')
    def test_account_link_after_physical_assignment_still_requires_explicit_access(self):
        from .crews import assign_jobs_to_crew
        from .models import ObjectAccess,ContractAccess
        crew=self.crew(self.worker,[]);a=self.assignment(crew);job=self.job()
        account=User.objects.create_user('+70000000006',None,role='foreman',full_name='Новый мастер',must_change_password=False)
        self.worker.user=account;self.worker.save()
        with self.assertRaises(ValidationError):assign_jobs_to_crew(self.tech,a.pk,{str(job.pk):job.version})
        a.refresh_from_db();self.assertIsNone(a.master_id)
        ObjectAccess.objects.create(user=account,project_object=self.obj,granted_by=self.leader)
        ContractAccess.objects.create(user=account,contract=self.contract,granted_by=self.leader)
        assign_jobs_to_crew(self.tech,a.pk,{str(job.pk):job.version})
        job.refresh_from_db();self.assertEqual(job.assigned_to,account)
    def test_equipment_export_hides_inaccessible_contract_and_crew(self):
        from .models import ContractAccess
        from accounts.models import AccessGrant
        crew=self.crew();a=self.assignment(crew);item=self.machine()
        fleet.assign(self.leader,item.pk,1,self.obj,self.contract,self.today,crew_assignment=a)
        AccessGrant.objects.create(user=self.tech,permission='equipment.manage',granted_by=self.leader)
        ContractAccess.objects.filter(user=self.tech).delete()
        row=equipment_excel.export_rows(self.tech,'assignments')[0]
        self.assertEqual(row[3],'');self.assertEqual(row[8],'')
    def test_employee_cannot_acknowledge_leaders_private_events(self):
        audit(self.leader,'finance_created',secret='private')
        e=AuditEvent.objects.latest('pk');self.client.force_login(self.master)
        self.client.post(reverse('business_inbox'),{'events':[e.pk]})
        self.assertFalse(BusinessNoticeRead.objects.filter(user=self.master).exists())
        self.assertNotContains(self.client.get(reverse('business_inbox')),'Финансовая запись')

class FeedbackBrowserTests(StaticLiveServerTestCase):
    def test_crew_bulk_assignment_without_login_and_notifications(self):
        import os,secrets,subprocess
        from django.conf import settings
        from documents.models import Person
        from .models import LegalEntity,COMPANY_TAX_ID,ProjectObject,Contract,ContractObject,Brigade
        from .crews import create_crew
        from django.utils import timezone
        company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})[0]
        password=secrets.token_urlsafe(24)
        leader=User.objects.create_user('+70000000631',password,leader_slot=1,full_name='ТЕСТ Руководитель',must_change_password=False)
        person=Person.objects.create(legal_entity=company,full_name='ТЕСТ Мастер без аккаунта',created_by=leader)
        crew=create_crew(leader,'ТЕСТ Бригада без аккаунта',person,[],timezone.localdate())
        contract=Contract.objects.create(legal_entity=company,number='ТЕСТ-К',subject='ТЕСТ',work_kinds=['repairs'])
        for name in ['ТЕСТ Объект А','ТЕСТ Объект Б']:
            obj=ProjectObject.objects.create(legal_entity=company,name=name);ContractObject.objects.create(contract=contract,project_object=obj)
        audit(leader,'contract_updated',entity_id=str(contract.pk),kind='closure_request')
        env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password,ARMADA_CREW=str(crew.pk),ARMADA_CONTRACT=str(contract.pk))
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/feedback-browser.cjs')],env=env,capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(BrigadeAssignment.objects.count(),2)
