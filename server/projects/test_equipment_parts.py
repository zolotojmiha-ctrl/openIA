import uuid
from decimal import Decimal
from urllib.parse import urlsplit,parse_qs
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import PermissionDenied,ValidationError
from accounts.presets import PRESETS
from accounts.models import AccessGrant,AuditEvent
from . import test_equipment as fixture,equipment_parts,equipment
from .models import EquipmentMaintenancePart,StockMovement


class EquipmentPartsTests(TestCase):
    setUpTestData=classmethod(fixture.EquipmentTests.setUpTestData.__func__)
    grant=fixture.EquipmentTests.grant
    make=fixture.EquipmentTests.make;data=fixture.EquipmentTests.data
    def values(self,**changes):return dict(name='Масляный фильтр',article='00123/45-AB',manufacturer='ТЕСТ Производитель',quantity=Decimal('1'),unit='шт.',note='Для ТО двигателя',archived=False)|changes
    def save(self,item,**changes):return equipment_parts.save(self.mechanic,item.pk,None,0,self.values(**changes),uuid.uuid4())
    def test_preset_can_register_equipment_but_role_alone_cannot(self):
        self.assertIn('equipment.manage',PRESETS['mechanic']['permissions'])
        self.assertNotIn('finance.write',PRESETS['mechanic']['permissions'])
        with self.assertRaises(PermissionDenied):self.make(actor=self.mechanic)
        self.grant();item=self.make(actor=self.mechanic)
        self.assertEqual(item.created_by,self.mechanic)
    def test_parts_require_explicit_grant_and_preserve_number_and_parent(self):
        item=self.make()
        with self.assertRaises(PermissionDenied):self.save(item)
        self.grant();part=self.save(item)
        part.refresh_from_db();self.assertEqual(part.article,'00123/45-AB');self.assertEqual(part.equipment_id,item.pk)
        self.assertEqual(part.quantity,Decimal('1'))
        self.assertEqual(AuditEvent.objects.filter(details__kind='maintenance_part').count(),1)
        self.assertFalse(StockMovement.objects.exists())
    def test_retry_edit_conflict_archive_and_restore_preserve_history(self):
        self.grant();item=self.make();token=uuid.uuid4()
        part=equipment_parts.save(self.mechanic,item.pk,None,0,self.values(),token)
        repeated=equipment_parts.save(self.mechanic,item.pk,None,0,self.values(quantity=Decimal('1.000')),token)
        self.assertEqual(repeated.pk,part.pk);self.assertEqual(EquipmentMaintenancePart.objects.count(),1)
        equipment_parts.save(self.mechanic,item.pk,part.pk,1,self.values(archived=True),token)
        with self.assertRaises(ValidationError):equipment_parts.save(self.mechanic,item.pk,part.pk,1,self.values(),token)
        equipment_parts.save(self.mechanic,item.pk,part.pk,2,self.values(),token)
        part.refresh_from_db();self.assertEqual(part.version,3);self.assertFalse(part.archived)
        self.assertEqual(AuditEvent.objects.filter(details__kind='maintenance_part').count(),3)
    def test_searches_encode_article_and_do_not_send_vehicle_or_contract(self):
        self.grant();item=self.make();part=self.save(item,article='00123 &/Б',manufacturer='ТЕСТ')
        links=equipment_parts.search_links(part)
        self.assertEqual(urlsplit(links['yandex']).netloc,'yandex.ru')
        self.assertEqual(parse_qs(urlsplit(links['yandex']).query)['text'],['ТЕСТ 00123 &/Б'])
        self.assertEqual(urlsplit(links['ozon']).netloc,'www.ozon.ru')
        self.assertEqual(urlsplit(links['wildberries']).netloc,'www.wildberries.ru')
        for url in links.values():self.assertNotIn(item.inventory_number,url)
    def test_archived_equipment_and_wrong_parent_cannot_be_changed(self):
        self.grant();item=self.make();part=self.save(item)
        other=self.make(inventory_number='TEST-OTHER')
        with self.assertRaises(EquipmentMaintenancePart.DoesNotExist):equipment_parts.save(self.mechanic,other.pk,part.pk,1,self.values(),uuid.uuid4())
        item.status='archived';item.save()
        with self.assertRaises(ValidationError):self.save(item)
    def test_http_create_revoke_and_viewer_permissions(self):
        self.grant();item=self.make();url=reverse('equipment_part_new',args=[item.pk])
        self.client.force_login(self.mechanic);self.assertEqual(self.client.get(url).status_code,200)
        payload={**self.values(),'token':uuid.uuid4(),'expected_version':0};payload.pop('archived')
        response=self.client.post(url,payload);self.assertEqual(response.status_code,302)
        part=EquipmentMaintenancePart.objects.get()
        page=self.client.get(reverse('equipment_detail',args=[item.pk]));self.assertContains(page,'Запчасти и материалы для ТО');self.assertContains(page,'00123/45-AB')
        self.assertContains(page,'Найти в Яндексе')
        AccessGrant.objects.filter(user=self.mechanic,permission='equipment.manage').delete()
        self.assertEqual(self.client.post(url,payload).status_code,403)
        self.assertEqual(self.client.get(reverse('equipment_part_edit',args=[item.pk,part.pk])).status_code,403)

    def test_employee_form_applies_preset_but_does_not_grant_by_role(self):
        from accounts.forms import EmployeeForm
        for profile,expected in [('mechanic',{'equipment.manage','document.upload'}),('custom',set())]:
            form=EmployeeForm({'full_name':'ТЕСТ Новый механик','phone':'+70000000006','role':'mechanic','profile':profile,'is_active':'on'},actor=self.leader)
            self.assertTrue(form.is_valid(),form.errors)
            self.assertEqual(set(form.cleaned_data['permissions']),expected)

    def test_viewer_sees_active_parts_but_cannot_edit_or_see_hidden_entries(self):
        from .models import EquipmentAssignment
        self.grant();item=self.make();part=self.save(item)
        EquipmentAssignment.objects.create(equipment=item,project_object=self.a,start_date=self.today,created_by=self.leader)
        self.client.force_login(self.worker)
        page=self.client.get(reverse('equipment_detail',args=[item.pk]))
        self.assertContains(page,'00123/45-AB');self.assertNotContains(page,'Добавить запчасть')
        self.assertEqual(self.client.get(reverse('equipment_part_edit',args=[item.pk,part.pk])).status_code,403)
        equipment_parts.save(self.mechanic,item.pk,part.pk,1,self.values(archived=True),uuid.uuid4())
        self.assertNotContains(self.client.get(reverse('equipment_detail',args=[item.pk])),'00123/45-AB')
