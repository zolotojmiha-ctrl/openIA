import importlib
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from django.apps import apps
from django.db import connection
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from . import test_stock as base,test_release624 as old,equipment,rentals,repair_materials,stock_planning
from .models import Equipment,EquipmentAssignment,EquipmentRental,StockMovement,StockItem
from repairs.models import Job,Report
from repairs import plans

class Release6241Tests(TestCase):
    setUpTestData=classmethod(base.StockTests.setUpTestData.__func__)
    wh=base.StockTests.wh;data=base.StockTests.data;item=base.StockTests.item;move=base.StockTests.move;crew=base.StockTests.crew;make_job=base.StockTests.make_job;concrete=base.StockTests.concrete;balance=base.StockTests.balance;report=base.StockTests.report
    def setUp(self):self.section.diameter_mm=720;self.section.save()
    def machine(self,ownership='rent'):
        return equipment.save_equipment(self.leader,None,None,dict(name='ТЕСТ Экскаватор',category='Экскаватор',inventory_number='6241',registration_number='ТЕСТ 6241',serial_number='',ownership=ownership,owner='ТЕСТ Арендодатель',status='ready',notes=''))
    def test_rental_is_automatic_and_hours_are_on_equipment_card(self):
        eq=self.machine();p=equipment.assign(self.leader,eq.pk,eq.version,self.obj,self.contract,self.today,crew_assignment=self.crew())
        row=EquipmentRental.objects.get(placement=p);self.assertEqual(row.supplier,eq.owner);self.assertFalse(row.days.exists())
        self.client.force_login(self.worker);url=reverse('equipment_detail',args=[eq.pk]);response=self.client.get(url)
        self.assertContains(response,'Наработка аренды');self.assertContains(response,'Работал — 10 ч');self.assertNotContains(response,'Включить учёт аренды');self.assertNotContains(response,'Взаиморасчёты')
        self.assertEqual(self.client.post(url,{'rental_id':row.pk,'action':'day','kind':'work','date':self.today,'version':0}).status_code,302)
        self.assertEqual(row.days.get().hours,10);self.assertIsNone(row.days.get().rate_id)
        self.assertContains(self.client.get(url),'Отметка за')
    def test_foreman_home_shows_pending_hours_once_and_other_pages_keep_reminder(self):
        eq=self.machine();equipment.assign(self.leader,eq.pk,eq.version,self.obj,self.contract,self.today,crew_assignment=self.crew())
        self.client.force_login(self.worker)
        home=self.client.get(reverse('dashboard'))
        self.assertContains(home,'Нужно отметить часы техники')
        self.assertContains(home,reverse('equipment_detail',args=[eq.pk])+'?rental='+str(EquipmentRental.objects.get(placement__equipment=eq).pk)+'#rental-hours',count=1)
        self.assertNotContains(home,'Открыть табель')
        self.assertContains(self.client.get(reverse('equipment')),'Открыть табель')
    def test_reminder_opens_the_correct_past_rental_period(self):
        yesterday=self.today-timedelta(days=1);crew=self.crew();crew.start_date=yesterday;crew.save(update_fields=['start_date'])
        eq=self.machine();past=equipment.assign(self.leader,eq.pk,eq.version,self.obj,self.contract,yesterday,end=yesterday,crew_assignment=crew)
        eq.refresh_from_db();current=equipment.assign(self.leader,eq.pk,eq.version,self.obj,self.contract,self.today,crew_assignment=crew)
        old=EquipmentRental.objects.get(placement=past);new=EquipmentRental.objects.get(placement=current)
        self.client.force_login(self.worker);url=reverse('equipment_detail',args=[eq.pk])
        home=self.client.get(reverse('dashboard'))
        self.assertContains(home,url+'?rental='+str(old.pk)+'#rental-hours')
        response=self.client.get(url,{'rental':str(old.pk)})
        self.assertEqual(response.context['rental'].pk,old.pk)
        self.assertEqual(response.context['day_form']['date'].value(),yesterday)
        self.assertEqual(self.client.get(url).context['rental'].pk,new.pk)
        self.assertEqual(self.client.get(url,{'rental':'invalid'}).status_code,404)
        saved=self.client.post(url,{'rental_id':old.pk,'action':'day','kind':'work','date':yesterday,'version':0})
        self.assertEqual(saved.status_code,302);self.assertEqual(saved.url,url+'?rental='+str(old.pk))
        self.assertEqual(old.days.get().hours,10);self.assertFalse(new.days.exists())
    def test_existing_assigned_machine_becomes_rental_without_activation(self):
        eq=self.machine('own');p=equipment.assign(self.leader,eq.pk,eq.version,self.obj,self.contract,self.today);eq.refresh_from_db()
        values={k:getattr(eq,k) for k in equipment.FIELDS};values['ownership']='rent'
        equipment.save_equipment(self.leader,eq.pk,eq.version,values)
        r=EquipmentRental.objects.get(placement=p);rentals.save_day(self.leader,r.pk,self.today,'external');eq.refresh_from_db()
        equipment.save_equipment(self.leader,eq.pk,eq.version,values)
        self.assertEqual(EquipmentRental.objects.count(),1);self.assertEqual(r.days.count(),1)
    def test_migration_adds_only_missing_active_rentals(self):
        eq=self.machine();p=EquipmentAssignment.objects.create(equipment=eq,project_object=self.obj,contract=self.contract,start_date=self.today,created_by=self.leader)
        migrate=importlib.import_module('projects.migrations.0027_automatic_rental_equipment').forwards
        for _ in range(2):migrate(apps,SimpleNamespace(connection=connection))
        self.assertEqual(EquipmentRental.objects.count(),1);r=EquipmentRental.objects.get(placement=p);self.assertFalse(r.days.exists());self.assertFalse(r.payments.exists())
    def test_main_navigation_has_no_rental_or_materials_sections(self):
        self.machine();self.client.force_login(self.leader);response=self.client.get(reverse('equipment'))
        self.assertNotContains(response,'>Аренда техники</a>');self.assertNotContains(response,'>Материалы по ремонтам</a>');self.assertContains(response,'name="ownership"')
        self.assertRedirects(self.client.get(reverse('rental_list')),'/cabinet/equipment/?ownership=rent')
    def test_no_new_equipment_visibility_for_customer(self):
        eq=self.machine();customer=old.Release624Tests.customer(self)
        self.assertFalse(equipment.visible_equipment(customer).exists());self.client.force_login(customer);self.assertEqual(self.client.get(reverse('equipment_detail',args=[eq.pk])).status_code,403)
    def reserved(self,length=1000):
        j=self.make_job();wh=self.wh(project_object=self.obj)
        from repairs.models import Coupling
        c=Coupling.objects.create(project_object=self.obj,method='p2',length_mm=length,wall_mm=8,serial='ТЕСТ 6241')
        i=self.item(kind='coupling',unit='pcs',method='p2',length_mm=length,wall_mm=8,diameter_mm=720,coupling=c);self.move(i,q=1,dst=wh)
        i=stock_planning.reserve(self.leader,i.pk,i.version,j.pk);return j,wh,i
    def change(self,j,length='1.5',version=None):
        values={k:getattr(j,k) for k in plans.FIELDS};values['length_m']=Decimal(length)
        return plans.revise(self.leader,j.pk,j.version if version is None else version,values,'ТЕСТ После обследования')
    def test_inspection_1000_to_1500_releases_only_reservation_and_keeps_stock(self):
        j,wh,item=self.reserved();j.source={'imported_original_length':'1'};j.save();version=item.version;movements=StockMovement.objects.count()
        j,released=self.change(j);item.refresh_from_db();self.assertEqual(j.length_m,Decimal('1.5'));self.assertIsNone(item.reserved_job_id);self.assertEqual(item.version,version+1)
        self.assertEqual(self.balance(wh,item),1);self.assertEqual(StockMovement.objects.count(),movements);self.assertEqual(len(released),1);self.assertEqual(j.source['imported_original_length'],'1')
        record=Report.objects.get(job=j,kind='plan_change');self.assertEqual(record.context['previous_plan']['length_m'],'1');self.assertEqual(record.context['new_plan']['length_m'],'1.5')
        demand=next(r for r in repair_materials.all_demand() if r['job']==j);self.assertEqual(demand['couplings'][0]['length'],1500);self.assertEqual(demand['couplings'][0]['missing'],1)
    def test_larger_reserved_piece_is_retained_when_new_requirement_fits(self):
        j,wh,item=self.reserved(1500);version=item.version;j,released=self.change(j)
        item.refresh_from_db();self.assertEqual(released,[]);self.assertEqual(item.reserved_job_id,j.pk);self.assertEqual(item.version,version);self.assertEqual(self.balance(wh,item),1)
    def test_generic_job_edit_also_rechecks_old_reservation(self):
        from repairs.test_planned_construction import PlannedInputTests
        j,wh,item=self.reserved();d=j.defects.get();fixture=PlannedInputTests();fixture.contract=self.contract;fixture.section=self.section
        payload=fixture.payload(expected_version=j.version,kilometre=str(d.kilometre),defect_numbers=d.number,length_m='1.5')
        self.client.force_login(self.leader);response=self.client.post(reverse('repair_edit',args=[j.pk]),payload)
        self.assertEqual(response.status_code,302,response.context and response.context['form'].errors);item.refresh_from_db();self.assertIsNone(item.reserved_job_id);self.assertEqual(self.balance(wh,item),1)
    def test_construction_change_rejects_stale_or_completed_work(self):
        j,wh,item=self.reserved();self.change(j)
        with self.assertRaises(ValidationError):self.change(j,version=1)
        j.refresh_from_db();j.state='confirmed';j.save()
        with self.assertRaises(ValidationError):self.change(j,'2')
        j.refresh_from_db();self.assertEqual(j.length_m,Decimal('1.5'))
    def test_foreman_cannot_rewrite_construction_requirement(self):
        j=self.make_job();self.client.force_login(self.worker);self.assertEqual(self.client.get(reverse('repair_construction',args=[j.pk])).status_code,403)
    def test_construction_form_is_short_and_records_reason(self):
        j,wh,item=self.reserved();j.composition='1';j.save();self.client.force_login(self.leader);url=reverse('repair_construction',args=[j.pk]);response=self.client.get(url);self.assertContains(response,'Основание изменения');self.assertEqual(response.context['form']['composition'].value(),'')
        payload={'expected_version':j.version,'method':'p2','length_m':'1.5','wall_mm':'8','composition':'','reason':'ТЕСТ Обследование'}
        self.assertEqual(self.client.post(url,payload).status_code,302);j.refresh_from_db();self.assertEqual(j.length_m,Decimal('1.5'));self.assertTrue(j.reports.filter(kind='plan_change',note='ТЕСТ Обследование').exists())

    def test_assigned_foreman_keeps_rental_access_without_extra_object_grant(self):
        from .models import ObjectAccess,ContractAccess
        eq=self.machine();p=equipment.assign(self.leader,eq.pk,eq.version,self.obj,self.contract,self.today,crew_assignment=self.crew())
        r=EquipmentRental.objects.get(placement=p)
        ObjectAccess.objects.filter(user=self.worker).delete();ContractAccess.objects.filter(user=self.worker).delete()
        self.client.force_login(self.worker);url=reverse('equipment_detail',args=[eq.pk])
        self.assertContains(self.client.get(url),'Наработка аренды')
        self.assertEqual(self.client.post(url,{'rental_id':r.pk,'action':'day','kind':'work','date':self.today,'version':0}).status_code,302)
        self.assertEqual(r.days.get().hours,10)
