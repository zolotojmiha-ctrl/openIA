import secrets,uuid
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from accounts.models import AccessGrant
from projects.models import Contract,ContractObject,ProjectObject,Equipment,EquipmentAssignment,ObjectAccess,ContractAccess
from projects import equipment_issues,rentals
from projects import test_stock as fixtures
from repairs.models import Job,Section
from . import tests as base,leadership,links
from .models import Binding,Outbox


@override_settings(**base.CONFIG)
class ScopedManagementTests(TestCase):
    setUpTestData=classmethod(fixtures.StockTests.setUpTestData.__func__)
    setUp=base.MaxTests.setUp
    bind=base.MaxTests.bind;event=base.MaxTests.event

    def request(self,text,user=None):
        user=user or self.leader
        binding=Binding.objects.filter(user=user,active=True).first() or self.bind(sender=301,chat=401,user=user)
        event=self.event(text,sender=binding.max_user,chat=binding.chat)
        return Outbox.objects.get(key=f'in:{event.pk}:reply'),binding

    def another(self):
        obj=ProjectObject.objects.create(legal_entity=self.company,name='ТЕСТ Чужой участок')
        contract=Contract.objects.create(legal_entity=self.company,number='ТЕСТ-ДРУГОЙ',subject='Тест',work_kinds=['repairs'])
        ContractObject.objects.create(contract=contract,project_object=obj)
        section=Section.objects.create(project_object=obj,rnu='ТЕСТ',pipeline='ТЕСТ',name='ТЕСТ Другая линия')
        job=Job.objects.create(section=section,contract=contract,method='grind',assigned_to=self.leader,created_by=self.leader)
        return contract,section,job

    def pto(self):
        self.worker.role='pto';self.worker.save();return self.worker

    def test_contract_and_section_selection_persists_and_filters(self):
        other,section,job=self.another()
        row,binding=self.request('scope:contract:'+str(self.contract.pk))
        row,_=self.request('leader:works:0')
        self.assertIn(self.contract.number,row.body['text']);self.assertNotIn(other.number,row.body['text'])
        row,_=self.request('scope:section:'+str(self.section.pk));binding.refresh_from_db()
        self.assertEqual(binding.menu_scope,{'contract':str(self.contract.pk),'section':str(self.section.pk)})
        row,_=self.request('scope:contract:all');binding.refresh_from_db();self.assertEqual(binding.menu_scope,{})
        row,_=self.request('leader:works:0');self.assertIn(other.number,row.body['text'])

    def test_pto_scope_cannot_be_forged_and_lost_access_cancels_queued_menu(self):
        user=self.pto();other,section,job=self.another()
        self.assertTrue(links.supported(user))
        row,binding=self.request('leader:works:0',user)
        self.assertNotIn(other.number,row.body['text'])
        with self.assertRaises(PermissionDenied):leadership.validate_scope(user,{'contract':str(other.pk)})
        self.assertTrue(leadership.delivery_allowed(user,row.scope))
        ContractAccess.objects.filter(user=user).delete()
        self.assertFalse(leadership.delivery_allowed(user,row.scope))
        self.client.force_login(user);self.assertContains(self.client.get(reverse('profile')),'Подключение MAX')

    def test_rental_summary_requires_finance_read_and_uses_real_ledger(self):
        machine=Equipment.objects.create(legal_entity=self.company,name='ТЕСТ аренда',category='Тест',inventory_number='AR-001',ownership='rent',created_by=self.leader)
        placement=EquipmentAssignment.objects.create(equipment=machine,project_object=self.obj,contract=self.contract,start_date=self.today,created_by=self.leader)
        hire=rentals.configure(self.leader,placement.pk,'ТЕСТ поставщик','ТЕСТ аренда')
        rentals.add_rate(self.leader,hire.pk,self.today,'day',Decimal(21000),Decimal(8400))
        rentals.save_day(self.leader,hire.pk,self.today,'work')
        rentals.payment(self.leader,hire.pk,self.today,Decimal(5000),'ТЕСТ оплата',uuid.uuid4())
        row,_=self.request('leader:rentals:0')
        for text in ('10 оплачиваемых ч','Начислено: 21 тыс. руб.','Оплачено: 5 тыс. руб.','К оплате: 16 тыс. руб.'):
            self.assertIn(text,row.body['text'])
        user=self.pto();row,_=self.request('/меню',user)
        self.assertNotIn('Аренда: наработка',str(row.body))
        row,_=self.request('leader:rentals:0',user)
        self.assertNotIn('21 тыс. руб.',row.body['text'])

    def test_issue_submission_is_idempotent_and_not_a_rental_or_status_change(self):
        machine=Equipment.objects.create(legal_entity=self.company,name='ТЕСТ техника',category='Тест',inventory_number='ISS-1',created_by=self.leader)
        placement=EquipmentAssignment.objects.create(equipment=machine,project_object=self.obj,contract=self.contract,start_date=self.today,created_by=self.leader)
        self.request('/меню');token=uuid.uuid4()
        issue=equipment_issues.submit(self.worker,machine.pk,placement.pk,'ТЕСТ Не заводится',token)
        self.assertEqual(equipment_issues.submit(self.worker,machine.pk,placement.pk,'ТЕСТ Не заводится',token).pk,issue.pk)
        machine.refresh_from_db();self.assertEqual(machine.status,'ready')
        notices=Outbox.objects.filter(key__startswith='leader:');self.assertEqual(notices.count(),1)
        self.assertIn(self.contract.number,notices.get().body['text'])
        with self.assertRaises(PermissionDenied):equipment_issues.resolve(self.worker,issue.pk,'Исправлено')
        equipment_issues.resolve(self.leader,issue.pk,'ТЕСТ Ремонт выполнен')
        self.assertFalse(leadership.delivery_allowed(self.leader,notices.get().scope))
        self.client.force_login(self.worker)
        self.assertContains(self.client.get(reverse('equipment_detail',args=[machine.pk])),'ТЕСТ Не заводится')

    def test_shortage_watch_deduplicates_and_changes_are_observed(self):
        from . import demand_watch
        self.request('/меню')
        with patch('maxbot.demand_watch.snapshot',return_value=('a'*64,[{'contract':'ТЕСТ','object':'ТЕСТ','section':'ТЕСТ','method':'p1','length':1000,'wall':'8','quantity':2}])):
            demand_watch.queue_changes();demand_watch.queue_changes()
        self.assertEqual(Outbox.objects.filter(key__startswith='shortage:').count(),1)
        with patch('maxbot.demand_watch.snapshot',return_value=('b'*64,[])):demand_watch.queue_changes()
        self.assertEqual(Outbox.objects.filter(key__startswith='shortage:').count(),2)

    def test_pto_reads_unassigned_work_in_scope_without_gaining_reporting_right(self):
        from repairs.access import visible_jobs
        from repairs.services import report_work
        user=self.pto();self.job.assigned_to=self.leader;self.job.save()
        self.assertTrue(visible_jobs(user).filter(pk=self.job.pk).exists())
        with self.assertRaises(PermissionDenied):
            report_work(user,self.job.pk,'result',{'token':uuid.uuid4(),'expected_version':self.job.version,'outcome':'complete','actual_coating_length_m':'2.4'})
