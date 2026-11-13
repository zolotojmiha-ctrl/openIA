import io
from datetime import timedelta
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import PermissionDenied
import zipfile
from xml.etree import ElementTree as ET
from accounts.models import AccessGrant
from . import test_equipment as fixture,rentals,equipment
from .models import ObjectAccess,ContractAccess,EquipmentAssignment


class MechanicHoursTests(TestCase):
    setUpTestData=classmethod(fixture.EquipmentTests.setUpTestData.__func__)
    grant=fixture.EquipmentTests.grant;make=fixture.EquipmentTests.make;data=fixture.EquipmentTests.data

    def setUp(self):
        self.grant();self.item=self.make(ownership='rent')
        self.placement=EquipmentAssignment.objects.create(equipment=self.item,project_object=self.a,contract=self.contract,
            start_date=self.today-timedelta(days=2),created_by=self.leader,note='СЕКРЕТ назначения')
        self.rental=rentals.configure(self.leader,self.placement.pk,'СЕКРЕТ арендодателя','СЕКРЕТ договора')
        rentals.add_rate(self.leader,self.rental.pk,self.placement.start_date,'day',Decimal('21000'),Decimal('8400'))
        rentals.save_day(self.leader,self.rental.pk,self.today-timedelta(days=1),'work')
        rentals.save_day(self.leader,self.rental.pk,self.today,'idle')
        ObjectAccess.objects.filter(user=self.mechanic).delete();ContractAccess.objects.filter(user=self.mechanic).delete()
        self.client.force_login(self.mechanic)

    def test_mechanic_reads_existing_hours_without_object_or_financial_access(self):
        listing=self.client.get(reverse('equipment'))
        self.assertContains(listing,'Наработка');self.assertNotContains(listing,'На участок не назначена')
        self.assertNotContains(listing,reverse('object_detail',args=[self.a.pk]))
        response=self.client.get(reverse('equipment_detail',args=[self.item.pk]))
        self.assertContains(response,'Наработка аренды');self.assertContains(response,'Табель')
        self.assertEqual(len(response.context['days']),2);self.assertEqual(response.context['technical_hours'],14)
        self.assertContains(response,self.a.name);self.assertContains(response,'Часов по табелю')
        self.assertNotContains(response,'Указать начало работы')
        self.assertNotContains(response,'Взаиморасчёты');self.assertNotContains(response,'СЕКРЕТ')
        self.assertNotContains(response,self.contract.number);self.assertNotContains(response,reverse('object_detail',args=[self.a.pk]))
        self.assertFalse(response.context['can_record_rental']);self.assertFalse(response.context['rental_financial'])
        self.assertFalse(any(day.can_change for day in response.context['days']))
        self.assertEqual(list(equipment.placements(self.mechanic)),[])
        self.assertTrue(equipment.operational_placements(self.mechanic).filter(pk=self.placement.pk).exists())

    def test_read_only_hours_export_has_no_amounts(self):
        response=self.client.get(reverse('rental_export',args=[self.rental.pk]))
        self.assertEqual(response.status_code,200)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            sheet=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        flat=' '.join(sheet.itertext())
        self.assertIn('Табель',flat);self.assertIn('Часы к оплате',flat);self.assertIn('Работал',flat)
        self.assertNotIn('Начислено',flat);self.assertNotIn('СЕКРЕТ',flat)
        self.assertNotIn('Платежи',flat)

    def test_mechanic_cannot_change_hours_or_finances_by_direct_request(self):
        for action,data in [('day',{'date':str(self.today),'kind':'work','version':1}),
                            ('rate',{}),('payment',{}),('info',{'version':1})]:
            response=self.client.post(reverse('rental_detail',args=[self.rental.pk]),{'action':action,**data})
            self.assertEqual(response.status_code,403)
        self.assertEqual(rentals.totals(self.rental)['hours'],14)
        with self.assertRaises(PermissionDenied):rentals.save_day(self.mechanic,self.rental.pk,self.today,'work',1,'ТЕСТ')

    def test_revoke_management_hides_hours_and_never_suggests_duplicate_assignment(self):
        AccessGrant.objects.filter(user=self.mechanic).delete()
        self.assertEqual(self.client.get(reverse('rental_detail',args=[self.rental.pk])).status_code,404)
        self.assertFalse(rentals.visible(self.mechanic).exists())
        # Another delegated equipment manager still needs their existing rental rights.
        self.mechanic.role='safety';self.mechanic.save();self.grant()
        response=self.client.get(reverse('equipment_detail',args=[self.item.pk]))
        self.assertNotContains(response,'Указать начало работы');self.assertContains(response,'Техника уже назначена')
        self.assertNotContains(response,'Табель')
