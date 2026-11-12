import uuid
from datetime import timedelta
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import ValidationError,PermissionDenied
from accounts.models import AuditEvent
from . import test_stock as base,equipment,rentals
from .models import Equipment,EquipmentAssignment,EquipmentRental


class EquipmentStartTests(TestCase):
    setUpTestData=classmethod(base.StockTests.setUpTestData.__func__)
    crew=base.StockTests.crew

    def setUp(self):
        self.assignment=self.crew()
        self.eq=Equipment.objects.create(legal_entity=self.company,name='ТЕСТ Кран',inventory_number='start-test',ownership='rent',created_by=self.leader)

    def place(self,start=None,end=None):
        self.eq.refresh_from_db()
        return equipment.assign(self.leader,self.eq.pk,self.eq.version,self.obj,self.contract,start or self.today,end,crew_assignment=self.assignment)

    def test_start_before_crew_and_end_after_crew_preserve_master_date_scope(self):
        self.assignment.end_date=self.today;self.assignment.save()
        start=self.today-timedelta(days=12)
        row=self.place(start,self.today+timedelta(days=5))
        rental=EquipmentRental.objects.get(placement=row)
        self.assertFalse(rentals.allowed_on(self.worker,rental,start))
        self.assertTrue(rentals.allowed_on(self.worker,rental,self.today))
        self.assertFalse(rentals.allowed_on(self.worker,rental,self.today+timedelta(days=1)))
        with self.assertRaises(PermissionDenied):rentals.save_day(self.worker,rental.pk,start,'work')
        rentals.save_day(self.leader,rental.pk,start,'work')

    def test_earlier_start_keeps_ledger_and_crew_and_audits_reason(self):
        row=self.place();rental=EquipmentRental.objects.get(placement=row)
        rate=rentals.add_rate(self.leader,rental.pk,self.today,'day',Decimal('21000'),Decimal('8400'))
        day=rentals.save_day(self.worker,rental.pk,self.today,'work')
        payment=rentals.payment(self.leader,rental.pk,self.today,Decimal('15500'),'ТЕСТ аванс',uuid.uuid4())
        before=rentals.totals(rental)
        start=self.today-timedelta(days=12)
        equipment.extend_start(self.leader,row.pk,row.version,start,'Уточнена фактическая дата')
        row.refresh_from_db();self.assignment.refresh_from_db();day.refresh_from_db()
        self.assertEqual(row.start_date,start);self.assertEqual(self.assignment.start_date,self.today)
        self.assertEqual(row.crew_assignment_id,self.assignment.pk)
        self.assertEqual(rentals.totals(rental),before)
        self.assertEqual((day.rate_id,day.amount),(rate.pk,Decimal('21000')))
        self.assertEqual(rental.payments.get().pk,payment.pk)
        self.assertEqual(rental.days.count(),1)
        rental.refresh_from_db();self.assertIn(start,rentals.missing_dates(rental,self.leader))
        earlier=rentals.add_rate(self.leader,rental.pk,start,'day',Decimal('20000'),Decimal('8000'))
        day.refresh_from_db();self.assertEqual(day.rate_id,rate.pk);self.assertEqual(day.amount,Decimal('21000'))
        self.assertEqual(rentals.save_day(self.leader,rental.pk,start,'work').rate_id,earlier.pk)
        self.assertTrue(AuditEvent.objects.filter(action='equipment_assigned',details__kind='start_corrected').exists())
        with self.assertRaises(ValidationError):equipment.extend_start(self.leader,row.pk,1,start-timedelta(days=1),'ТЕСТ')

    def test_overlap_forward_date_and_empty_reason_rejected(self):
        row=self.place()
        EquipmentAssignment.objects.create(equipment=self.eq,project_object=self.obj,start_date=self.today-timedelta(days=20),end_date=self.today-timedelta(days=5),created_by=self.leader)
        for start,reason in [(self.today-timedelta(days=10),'ТЕСТ'),(self.today+timedelta(days=1),'ТЕСТ'),(self.today-timedelta(days=1),'')]:
            with self.assertRaises(ValidationError):equipment.extend_start(self.leader,row.pk,row.version,start,reason)
        row.refresh_from_db();self.assertEqual(row.start_date,self.today)
        with self.assertRaises(PermissionDenied):equipment.extend_start(self.worker,row.pk,row.version,self.today-timedelta(days=1),'ТЕСТ')

    def test_card_form_and_creation_http(self):
        self.client.force_login(self.leader)
        url=reverse('equipment_assign',args=[self.eq.pk]);start=self.today-timedelta(days=12)
        response=self.client.post(url,{'expected_version':self.eq.version,'project_object':self.obj.pk,'contract':self.contract.pk,'crew_assignment':self.assignment.pk,'start_date':start.isoformat()})
        self.assertEqual(response.status_code,302)
        row=self.eq.placements.get();card=reverse('equipment_detail',args=[self.eq.pk])
        self.assertContains(self.client.get(card),'Исправить начало работы')
        url=reverse('equipment_assignment_start',args=[row.pk])
        self.assertEqual(self.client.post(url,{'expected_version':row.version,'start_date':(start-timedelta(days=1)).isoformat(),'reason':'ТЕСТ исправление'}).status_code,302)
        self.client.force_login(self.worker);self.assertEqual(self.client.get(url).status_code,403)

    def test_crew_can_finish_while_equipment_keeps_working(self):
        from .crews import close_assignment
        row=self.place()
        close_assignment(self.leader,self.assignment.pk,self.assignment.version,self.today)
        row.refresh_from_db();self.assertIsNone(row.end_date)
        rental=EquipmentRental.objects.get(placement=row)
        self.assignment.refresh_from_db();rental.refresh_from_db()
        self.assertFalse(rentals.allowed_on(self.worker,rental,self.today+timedelta(days=1)))
