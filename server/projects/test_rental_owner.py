import io,uuid,zipfile
from datetime import timedelta
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import PermissionDenied,ValidationError
from accounts.models import User,AccessGrant
from accounts.forms import EmployeeForm
from projects.models import RentalAccess,RentalDispute,RentalDay,Equipment,EquipmentAssignment
from projects import test_stock as fixture,test_release624 as sample,rentals,rental_owner

class RentalOwnerTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    crew=fixture.StockTests.crew;rental=sample.Release624Tests.rental
    def setUp(self):
        self.hire=self.rental()
        machine=Equipment.objects.create(legal_entity=self.company,name='ТЕСТ чужая техника',inventory_number='OTHER',category='ТЕСТ',ownership='rent',created_by=self.leader)
        placement=EquipmentAssignment.objects.create(equipment=machine,project_object=self.obj,contract=self.contract,crew_assignment=self.hire.placement.crew_assignment,start_date=self.hire.placement.start_date,created_by=self.leader)
        self.other=rentals.configure(self.leader,placement.pk,'ТЕСТ другой арендодатель','ТЕСТ другой договор')
        self.owner=User.objects.create_user('+70000000004',role='lessor',full_name='ТЕСТ арендодатель',must_change_password=False)
        self.access=rental_owner.grant(self.leader,self.hire.pk,self.owner.pk,0,True,False)
        rentals.save_day(self.worker,self.hire.pk,self.today,'work')
        self.client.force_login(self.owner);self.url=reverse('lessor_detail',args=[self.hire.pk])
    def test_reads_only_granted_rental_and_default_has_no_money(self):
        response=self.client.get(self.url);self.assertEqual(response.status_code,200)
        self.assertContains(response,'Работал');self.assertNotContains(response,'Начислено');self.assertNotContains(response,self.hire.reference)
        self.assertEqual(self.client.get(reverse('lessor_detail',args=[self.other.pk])).status_code,404)
        self.assertNotContains(response,'Контракты с заказчиками');self.assertNotContains(response,'Персонал')
        self.assertEqual(self.client.get(reverse('lessor_home')).context['accesses'][0].rental_id,self.hire.pk)
        self.assertRedirects(self.client.get(reverse('dashboard')),reverse('lessor_home'))
    def test_financial_toggle_is_per_grant_and_rechecked_for_exports(self):
        response=self.client.get(reverse('lessor_export',args=[self.hire.pk]))
        with zipfile.ZipFile(io.BytesIO(response.content)) as book:
            sheet=book.read('xl/worksheets/sheet1.xml').decode();self.assertNotIn('Начислено',sheet);self.assertIn('t="n"',sheet)
        rental_owner.grant(self.leader,self.hire.pk,self.owner.pk,self.access.version,True,True)
        self.assertContains(self.client.get(self.url),'Начислено')
        self.assertEqual(self.client.get(reverse('lessor_detail',args=[self.other.pk])).status_code,404)
    def test_internal_routes_remain_blocked_even_with_accidental_grants(self):
        AccessGrant.objects.create(user=self.owner,permission='finance.read',granted_by=self.leader)
        AccessGrant.objects.create(user=self.owner,permission='equipment.manage',granted_by=self.leader)
        for name,args in [('repairs',[]),('finance',[]),('users',[]),('equipment_detail',[self.hire.placement.equipment_id]),('rental_detail',[self.hire.pk]),('rental_export',[self.hire.pk]),('rental_access',[self.hire.pk])]:
            self.assertEqual(self.client.get(reverse(name,args=args)).status_code,403,name)
        self.assertFalse(rentals.manager(self.owner));self.assertFalse(rentals.financial_reader(self.owner))
    def test_dispute_never_changes_ledger_and_retries_do_not_duplicate(self):
        before=list(self.hire.days.values_list('date','kind','hours','amount','version'))
        token=uuid.uuid4();first=rental_owner.dispute(self.owner,self.access.pk,self.today,'ТЕСТ проверьте часы',token)
        self.assertEqual(rental_owner.dispute(self.owner,self.access.pk,self.today,'ТЕСТ проверьте часы',token),first)
        self.assertEqual(list(self.hire.days.values_list('date','kind','hours','amount','version')),before)
        with self.assertRaises(ValidationError):rental_owner.dispute(self.owner,self.access.pk,self.today+timedelta(days=1),'ТЕСТ',uuid.uuid4())
    def test_revoke_immediately_closes_read_and_dispute(self):
        rental_owner.grant(self.leader,self.hire.pk,self.owner.pk,self.access.version,False,False)
        self.assertEqual(self.client.get(self.url).status_code,404)
        with self.assertRaises(PermissionDenied):rental_owner.dispute(self.owner,self.access.pk,self.today,'ТЕСТ',uuid.uuid4())
    def test_only_leader_can_grant_and_stale_edits_fail(self):
        with self.assertRaises(PermissionDenied):rental_owner.grant(self.worker,self.other.pk,self.owner.pk,0,True,True)
        with self.assertRaises(ValidationError):rental_owner.grant(self.leader,self.hire.pk,self.owner.pk,0,True,True)
    def test_technical_user_reviews_dispute_without_owner_editing_days(self):
        dispute=rental_owner.dispute(self.owner,self.access.pk,self.today,'ТЕСТ проверьте',uuid.uuid4())
        self.client.force_login(self.leader)
        response=self.client.get(reverse('equipment_detail',args=[self.hire.placement.equipment_id]),{'rental':str(self.hire.pk)})
        self.assertContains(response,'ТЕСТ проверьте')
        response=self.client.post(reverse('rental_dispute_resolve',args=[dispute.pk]),{'resolution':'ТЕСТ сверено'})
        self.assertEqual(response.status_code,302);dispute.refresh_from_db();self.assertEqual(dispute.resolution,'ТЕСТ сверено')
    def test_external_role_form_rejects_internal_permissions_and_corporate_mail(self):
        data={'full_name':'ТЕСТ','phone':'+70000000009','role':'lessor','profile':'lessor','is_active':True,'permissions':['finance.read']}
        form=EmployeeForm(data,actor=self.leader);self.assertFalse(form.is_valid())

    def test_disabled_owner_grant_can_still_be_revoked_by_leader(self):
        self.owner.is_active=False;self.owner.save(update_fields=['is_active'])
        self.assertFalse(rental_owner.visible(self.owner).exists())
        result=rental_owner.grant(self.leader,self.hire.pk,self.owner.pk,self.access.version,False,False)
        self.assertFalse(result.active)
        with self.assertRaises(ValidationError):rental_owner.grant(self.leader,self.hire.pk,self.owner.pk,result.version,True,False)
