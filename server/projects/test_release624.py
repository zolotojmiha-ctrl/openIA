import io,tempfile,uuid,zipfile
from datetime import timedelta
from decimal import Decimal
from django.test import TestCase,override_settings
from django.core.exceptions import ValidationError,PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from accounts.models import User,AccessGrant
from . import test_stock as base, rentals, repair_materials as materials, stock
from .models import (Equipment,EquipmentAssignment,EquipmentRental,BrigadeAssignment,StockItem,StockMovement,
 CoatingProfile,RepairConsumption,CustomerAccess,CustomerPublication,CustomerDocumentLink,ContractObject,ProjectObject)
from repairs.models import Report,Job
from documents.models import Document

class Release624Tests(TestCase):
    setUpTestData=classmethod(base.StockTests.setUpTestData.__func__)
    wh=base.StockTests.wh;data=base.StockTests.data;item=base.StockTests.item;move=base.StockTests.move
    balance=base.StockTests.balance;make_job=base.StockTests.make_job;concrete=base.StockTests.concrete;crew=base.StockTests.crew;report=base.StockTests.report
    def setUp(self):
        self.section.diameter_mm=720;self.section.save()
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        override=override_settings(PRIVATE_DOCUMENT_ROOT=self.tmp.name);override.enable();self.addCleanup(override.disable)
    def rental(self):
        crew=self.crew();crew.start_date=self.today-timedelta(days=10);crew.save()
        eq=Equipment.objects.create(legal_entity=self.company,name='ТЕСТ Экскаватор',inventory_number='624',category='Экскаватор',registration_number='ТЕСТ 001',ownership='rent',created_by=self.leader)
        p=EquipmentAssignment.objects.create(equipment=eq,project_object=self.obj,contract=self.contract,crew_assignment=crew,start_date=self.today-timedelta(days=3),created_by=self.leader)
        row=rentals.configure(self.leader,p.pk,'ТЕСТ Арендодатель','ТЕСТ АР-624')
        rentals.add_rate(self.leader,row.pk,p.start_date,'hour',Decimal('100'),Decimal('80'))
        return row
    def profile(self,length=1000):
        wh=self.wh(project_object=self.obj);items={}
        for n,key in enumerate(['tape','wrapper','primer']):
            i=self.item(sku=str(n),name=key,kind={'tape':'ksizol','wrapper':'wrapper','primer':'primer'}[key]);self.move(i,dst=wh,q=100);items[key]=i
        p=CoatingProfile.objects.create(method='p2',diameter_mm=720,length_mm=length,full_length_mm=length+460,area_per_m='2.55',basis='ТЕСТ Подтверждённая карта',created_by=self.leader,**items)
        return p,wh,items
    def customer(self):
        u=User.objects.create_user('+70000000624','123456',role='customer',full_name='ТЕСТ Заказчик',must_change_password=False)
        CustomerAccess.objects.create(user=u,contract=self.contract,project_object=self.obj,created_by=self.leader);return u

    def test_rental_work_idle_zero_and_idempotent_click(self):
        r=self.rental()
        for offset,kind,hours,amount in [(2,'work',10,1000),(1,'idle',4,320),(0,'external',0,0)]:
            date=self.today-timedelta(days=offset)
            for _ in range(2):d=rentals.save_day(self.worker,r.pk,date,kind)
            self.assertEqual((d.hours,d.amount),(hours,amount));self.assertEqual(d.revisions.count(),1)
        self.assertEqual(rentals.totals(r)['charged'],1320)
        self.assertEqual(len(rentals.missing_dates(r)),1)
    def test_unreported_day_is_not_zero_or_idle(self):
        r=self.rental();self.assertEqual(rentals.totals(r)['charged'],0);self.assertEqual(len(rentals.missing_dates(r)),4)
        with self.assertRaises(ValidationError):rentals.close_period(self.leader,r.pk,r.placement.start_date,self.today)
    def test_master_scope_and_backdated_day(self):
        r=self.rental();self.assertTrue(rentals.visible(self.worker).filter(pk=r.pk).exists())
        with self.assertRaises(EquipmentRental.DoesNotExist):rentals.save_day(self.supply,r.pk,self.today,'work')
        self.assertEqual(rentals.save_day(self.worker,r.pk,self.today-timedelta(days=2),'work').hours,10)
        with self.assertRaises(ValidationError):rentals.save_day(self.worker,r.pk,self.today-timedelta(days=5),'work')
        with self.assertRaises(ValidationError):rentals.save_day(self.worker,r.pk,self.today+timedelta(days=1),'work')
    def test_day_correction_has_reason_and_revision(self):
        r=self.rental();d=rentals.save_day(self.worker,r.pk,self.today,'work')
        with self.assertRaises(ValidationError):rentals.save_day(self.worker,r.pk,self.today,'external',d.version)
        d=rentals.save_day(self.worker,r.pk,self.today,'external',d.version,'ТЕСТ поломка арендодателя');self.assertEqual((d.amount,d.revisions.count()),(0,2))
    def test_prepayment_partial_payment_and_period_carry(self):
        r=self.rental();rentals.save_day(self.worker,r.pk,self.today,'work')
        token=uuid.uuid4()
        for _ in range(2):rentals.payment(self.leader,r.pk,self.today,Decimal(300),'ТЕСТ аванс',token)
        self.assertEqual((rentals.totals(r)['debt'],r.payments.count()),(700,1))
        rentals.close_period(self.leader,r.pk,self.today,self.today,'ТЕСТ сверка')
        self.assertEqual(rentals.totals(r)['debt'],700)
        with self.assertRaises(ValidationError):rentals.save_day(self.worker,r.pk,self.today,'external',1,'ТЕСТ')
        rentals.payment(self.leader,r.pk,self.today,Decimal(900),'ТЕСТ оплата',uuid.uuid4())
        self.assertEqual((rentals.totals(r)['debt'],rentals.totals(r)['advance']),(0,200))
    def test_tariff_cannot_rewrite_existing_day(self):
        r=self.rental();rentals.save_day(self.worker,r.pk,self.today,'work')
        with self.assertRaises(ValidationError):rentals.add_rate(self.leader,r.pk,self.today,'day',Decimal(200),Decimal(20))
        self.assertEqual(rentals.totals(r)['charged'],1000)
    def test_day_tariffs_not_proportional(self):
        r=self.rental();rentals.add_rate(self.leader,r.pk,self.today,'day',Decimal(1000),Decimal(700))
        self.assertEqual(rentals.save_day(self.worker,r.pk,self.today,'idle').amount,700)
    def test_master_cannot_access_rates_payments_or_money_export(self):
        r=self.rental();rentals.save_day(self.worker,r.pk,self.today,'work')
        self.client.force_login(self.worker);url=reverse('rental_detail',args=[r.pk]);response=self.client.get(url)
        self.assertEqual(response.status_code,200);self.assertNotContains(response,'Взаиморасчёты');self.assertNotContains(response,'1000,00')
        self.assertEqual(self.client.post(url,{'action':'payment','date':self.today,'amount':1,'note':'bad','token':uuid.uuid4()}).status_code,403)
        self.assertEqual(self.client.get(reverse('rental_export',args=[r.pk])).status_code,200)
    def test_leader_rental_ui_and_profile_pages_render(self):
        r=self.rental();self.client.force_login(self.leader)
        for name,args in [('rental_list',[]),('rental_detail',[r.pk]),('coating_profiles',[]),('repair_materials',[]),('rental_setup',[])]:
            response=self.client.get(reverse(name,args=args),follow=True);self.assertEqual(response.status_code,200,(name,response.content[:500]))
    def test_kit_tables_have_normative_allowance_once(self):
        self.assertEqual(materials.kits(720,1500),{'sealant':2,'composite':7})
        self.assertEqual(materials.kits(720,2500),{'sealant':2,'composite':11})
        with self.assertRaises(ValidationError):materials.kits(999,1500)
    def test_coating_profile_uses_full_dimensions_and_accepted_rates(self):
        p,wh,items=self.profile(1500);j=self.make_job();j.length_m=Decimal('1.5');j.save()
        lines=materials.norm(j);self.assertEqual({Decimal(l['area']) for l in lines},{Decimal('7.548')})
        self.assertEqual({l['kind']:l['quantity'] for l in lines},{'tape':'39.175','wrapper':'6.114','primer':'1.586'})
    def test_missing_coating_does_not_fake_zero_or_block_repair(self):
        wh=self.wh(project_object=self.obj);c,i=self.concrete();self.move(i,q=1,dst=wh);j=self.make_job();self.report(j,[c])
        row=RepairConsumption.objects.get(job=j);self.assertEqual(row.state,'pending');self.assertIn('Не настроена',row.message);self.assertEqual(self.balance(wh,i),0)
    def test_materials_post_once_and_survive_report_retry(self):
        profile,wh,items=self.profile();c,i=self.concrete();self.move(i,q=1,dst=wh);j=self.make_job();token=uuid.uuid4()
        a=self.report(j,[c],token=token);j.refresh_from_db()
        # Photo/note correction of the same physical installation does not consume twice.
        self.report(j,[c],action='correction')
        self.assertEqual(StockMovement.objects.filter(job=j,kind='consume',item__unit='kg').count(),3)
        self.assertEqual(RepairConsumption.objects.get(job=j).state,'posted')
    def test_material_shortage_is_pending_without_partial_consumption(self):
        p,wh,items=self.profile();self.move(items['primer'],'loss',q=100,src=wh)
        c,i=self.concrete();self.move(i,q=1,dst=wh);j=self.make_job();self.report(j,[c])
        self.assertEqual(RepairConsumption.objects.get(job=j).state,'pending')
        self.assertEqual(self.balance(wh,items['tape']),100)
        self.assertFalse(StockMovement.objects.filter(job=j,item__unit='kg',kind='consume').exists())
        self.move(items['primer'],dst=wh,q=100);materials.retry(self.leader,j)
        self.assertEqual(RepairConsumption.objects.get(job=j).state,'posted')
        with self.assertRaises(ValidationError):materials.retry(self.leader,j)
    def test_shared_free_coupling_is_not_counted_twice(self):
        wh=self.wh();i=self.item(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8,diameter_mm=720);self.move(i,q=1,dst=wh)
        a=self.make_job();b=self.make_job();rows=materials.all_demand()
        self.assertEqual(sum(n['missing'] for r in rows for n in r['couplings']),1)
    def test_global_matching_can_reassign_oversized_piece(self):
        wh=self.wh();a=self.make_job();b=self.make_job();b.length_m=Decimal('1.5');b.save()
        for n,length in enumerate([1000,1500]):
            i=self.item(sku=str(n),kind='coupling',unit='pcs',method='p2',length_mm=length,wall_mm=8,diameter_mm=720);self.move(i,q=1,dst=wh)
        self.assertEqual(sum(n['missing'] for r in materials.all_demand() for n in r['couplings']),0)
    def test_customer_preset_role_and_scope_form(self):
        from accounts.forms import EmployeeForm
        scope=ContractObject.objects.get(contract=self.contract,project_object=self.obj)
        f=EmployeeForm({'full_name':'ТЕСТ Заказчик','phone':'+70000000625','role':'customer','profile':'customer','is_active':'on','customer_scopes':[scope.pk],'customer_scopes_present':'on'},actor=self.leader)
        self.assertTrue(f.is_valid(),f.errors);self.assertEqual(f.cleaned_data['permissions'],[])
        self.client.force_login(self.leader);response=self.client.post(reverse('employee_new'),f.data);self.assertEqual(response.status_code,200)
        u=User.objects.get(phone='+70000000625');self.assertTrue(CustomerAccess.objects.filter(user=u,contract=self.contract,project_object=self.obj).exists())
    def test_customer_document_upload_and_scope_isolation(self):
        u=self.customer();url=reverse('customer_documents',args=[self.contract.pk,self.obj.pk]);self.client.force_login(u)
        self.assertEqual(self.client.post(url,{'file':SimpleUploadedFile('ppr.txt',b'synthetic private exchange'),'title':'ТЕСТ ППР'}).status_code,302)
        link=CustomerDocumentLink.objects.get();response=self.client.get(reverse('customer_document_download',args=[link.pk]));self.assertEqual(b''.join(response.streaming_content),b'synthetic private exchange')
        self.client.force_login(self.worker);self.assertEqual(self.client.get(reverse('customer_document_download',args=[link.pk])).status_code,403)
        CustomerAccess.objects.filter(user=u).delete();self.client.force_login(u);self.assertEqual(self.client.get(reverse('customer_document_download',args=[link.pk])).status_code,404)
    def test_customer_portal_and_excel_never_expose_stock_or_photos(self):
        u=self.customer();self.make_job()
        CustomerPublication.objects.create(contract=self.contract,project_object=self.obj,created_by=self.leader,snapshot={'rows':[{'pipeline':'ТЕСТ','km':'1','number':'A','state':'Устранён','latitude':'55.123456','longitude':'73.123456'}],'photos':['anything']})
        self.client.force_login(u);response=self.client.get(reverse('customer_portal'));self.assertEqual(response.status_code,200)
        self.assertContains(response,'google.com/maps?q=55.123456,73.123456');self.assertNotContains(response,'Фотографии');self.assertNotContains(response,'На складе')
        self.assertEqual(self.client.get(reverse('customer_excel',args=[self.contract.pk,self.obj.pk])).status_code,200)
    def test_rental_unpriced_hours_are_saved_and_first_rate_prices_them(self):
        r=self.rental();r.rates.all().delete()
        d=rentals.save_day(self.worker,r.pk,self.today,'work');self.assertEqual((d.hours,d.amount),(10,0));self.assertEqual(rentals.totals(r)['unpriced'],1)
        with self.assertRaises(ValidationError):rentals.close_period(self.leader,r.pk,self.today,self.today)
        rentals.add_rate(self.leader,r.pk,r.placement.start_date,'hour',Decimal(100),Decimal(80));d.refresh_from_db()
        self.assertEqual(d.amount,1000);self.assertEqual(d.revisions.count(),2);self.assertEqual(rentals.totals(r)['unpriced'],0)
    def test_reopen_keeps_closed_snapshot_and_day_revisions(self):
        r=self.rental();rentals.save_day(self.worker,r.pk,self.today,'work')
        period=rentals.close_period(self.leader,r.pk,self.today,self.today);original=period.snapshot
        with self.assertRaises(ValidationError):rentals.reopen_period(self.leader,r.pk,period.pk,'')
        rentals.reopen_period(self.leader,r.pk,period.pk,'ТЕСТ сверка');rentals.save_day(self.worker,r.pk,self.today,'external',1,'ТЕСТ исправление')
        period.refresh_from_db();self.assertEqual(period.snapshot,original);self.assertEqual(rentals.totals(r)['charged'],0)
        self.assertNotEqual(rentals.close_period(self.leader,r.pk,self.today,self.today).pk,period.pk)
    def test_technician_can_verify_but_not_change_money(self):
        r=self.rental();AccessGrant.objects.create(user=self.supply,permission='rental.manage',granted_by=self.leader)
        self.assertTrue(rentals.financial_reader(self.supply));rentals.save_day(self.supply,r.pk,self.today,'work')
        with self.assertRaises(PermissionDenied):rentals.payment(self.supply,r.pk,self.today,Decimal(1),'ТЕСТ',uuid.uuid4())
    def test_method_rules_cover_import_shared_validation(self):
        from repairs.construction import planned_parts
        with self.assertRaises(ValidationError):planned_parts(2,'1+1','p2')
        with self.assertRaises(ValidationError):planned_parts(11,'','p1')
        self.assertEqual(planned_parts('2.5','1+1.5','p1')[0],Decimal('2.5'))
    def test_p1_composer_reserves_same_wall_without_changing_total(self):
        from .p1_composition import choices,apply
        wh=self.wh(project_object=self.obj);j=self.make_job();j.method='p1';j.length_m=Decimal('2.5');j.save()
        for n,(length,wall) in enumerate([(1000,8),(1500,8),(1000,10)]):
            i=self.item(sku=str(n),kind='coupling',unit='pcs',method='p1',length_mm=length,wall_mm=wall,diameter_mm=720);self.move(i,q=1,dst=wh)
        variants=choices(j,self.leader);self.assertEqual(len(variants),1)
        apply(self.leader,j.pk,variants[0]['token']);j.refresh_from_db()
        self.assertEqual(j.length_m,Decimal('2.5'));self.assertEqual(j.composition,'1+1.5');self.assertEqual(j.reserved_stock.count(),2)
        self.assertEqual(set(j.reserved_stock.values_list('wall_mm',flat=True)),{Decimal(8)})
        with self.assertRaises(ValidationError):apply(self.leader,j.pk,variants[0]['token'])
    def test_p1_composer_never_mixes_walls(self):
        from .p1_composition import choices
        wh=self.wh(project_object=self.obj);j=self.make_job();j.method='p1';j.length_m=Decimal('2.5');j.save()
        for n,(length,wall) in enumerate([(1000,8),(1500,10)]):
            i=self.item(sku=str(n),kind='coupling',unit='pcs',method='p1',length_mm=length,wall_mm=wall,diameter_mm=720);self.move(i,q=1,dst=wh)
        self.assertEqual(choices(j),[])
    def test_pto_consumption_excel_does_not_require_full_stock_access(self):
        AccessGrant.objects.create(user=self.worker,permission='repairs.prepare',granted_by=self.leader)
        wh=self.wh(project_object=self.obj);item=self.item(name='ТЕСТ Фактический расход');self.move(item,dst=wh,q=10);job=self.make_job()
        movement=self.move(item,'consume',3,src=wh,job=job);stock.reverse(self.leader,movement.pk,'ТЕСТ ошибочное списание',uuid.uuid4())
        self.client.force_login(self.worker);response=self.client.get(reverse('repair_materials_export'));self.assertEqual(response.status_code,200)
        from repairs.importer import read_book
        cells=read_book(SimpleUploadedFile('expense.xlsx',response.content))['cells']
        amounts=[Decimal(cells[(r,9)]) for r,c in cells if c==7 and cells[(r,c)]==item.name]
        self.assertCountEqual(amounts,[Decimal(3),Decimal(-3)]);self.assertEqual(sum(amounts),0)
        self.assertContains(self.client.get(reverse('repair_detail',args=[job.pk])),'Материалы этого ремонта')
    def test_rental_financial_summary_includes_charge_and_payment_once(self):
        from finance.models import FinancialScope
        from finance.services import summary
        scope=FinancialScope.objects.create(legal_entity=self.company,contract=self.contract,title='ТЕСТ Аренда',created_by=self.leader)
        row=self.rental();rentals.save_day(self.worker,row.pk,self.today,'work');rentals.payment(self.leader,row.pk,self.today,Decimal(300),'ТЕСТ аванс',uuid.uuid4())
        card=summary(scope);self.assertEqual((card['direct'],card['cash_out']),(1000,300));self.assertEqual(scope.costs.count(),0)
        self.client.force_login(self.leader);self.assertEqual(self.client.get(reverse('finance_detail',args=[scope.pk])).status_code,200)
    def test_customer_document_marks_read_and_hides_withdrawn_version(self):
        u=self.customer();self.client.force_login(self.leader);url=reverse('customer_documents',args=[self.contract.pk,self.obj.pk])
        self.client.post(url,{'file':SimpleUploadedFile('ppr.txt',b'TEST private file')})
        self.client.force_login(u)
        link=CustomerDocumentLink.objects.select_related('document__current_version').get()
        self.assertContains(self.client.get(url),'Новый документ')
        self.assertContains(self.client.get(reverse('customer_portal')),'новых 1')
        self.client.get(reverse('customer_document_download',args=[link.pk]))
        self.assertNotContains(self.client.get(url),'Новый документ')
        self.assertNotContains(self.client.get(reverse('customer_portal')),'новых 1')
        version=link.document.current_version;version.status='revision';version.save()
        self.assertEqual(self.client.get(reverse('customer_document_download',args=[link.pk])).status_code,404)
    def test_new_p2_composite_is_rejected_by_manual_input_and_import_review(self):
        from repairs.forms import JobForm
        from repairs.import_views import ReviewForm
        from repairs.test_planned_construction import PlannedInputTests
        fixture=PlannedInputTests();fixture.contract=self.contract;fixture.section=self.section;fixture.leader=self.leader
        form=JobForm(fixture.payload(length_m=2,composition='1+1'),user=self.leader)
        self.assertFalse(form.is_valid());self.assertIn('П2 нельзя стыковать',str(form.errors))
        batch=fixture.batch(length_m='2',composition='1+1')
        self.client.force_login(self.leader);response=self.client.post(reverse('repair_import_review',args=[batch.pk]),{'row_0':'new','length_0':'2','composition_0':'1+1'})
        self.assertEqual(response.status_code,200);self.assertFalse(Job.objects.exists());self.assertContains(response,'П2 нельзя стыковать')
    def test_p1_reservation_rejects_different_walls_before_installation(self):
        from . import stock_planning
        wh=self.wh(project_object=self.obj);j=self.make_job();j.method='p1';j.length_m=Decimal('2.5');j.composition='1+1.5';j.save()
        from repairs.models import Coupling
        for n,(length,wall) in enumerate([(1000,8),(1500,10)]):
            c=Coupling.objects.create(project_object=self.obj,method='p1',length_mm=length,wall_mm=wall,serial='ТЕСТ-'+str(n));i=self.item(sku='c'+str(n),kind='coupling',unit='pcs',method='p1',length_mm=length,wall_mm=wall,diameter_mm=720,coupling=c);self.move(i,q=1,dst=wh)
            if n==0:stock_planning.reserve(self.leader,i.pk,i.version,j.pk)
            else:
                with self.assertRaises(ValidationError):stock_planning.reserve(self.leader,i.pk,i.version,j.pk)
        self.assertEqual(j.reserved_stock.count(),1)
    def test_unpriced_rental_does_not_claim_a_financial_profit(self):
        from finance.models import FinancialScope
        from finance.services import summary
        scope=FinancialScope.objects.create(legal_entity=self.company,contract=self.contract,title='ТЕСТ Аренда',created_by=self.leader)
        row=self.rental();rentals.save_day(self.worker,row.pk,self.today,'work')
        row.rates.create(starts_on=self.today+timedelta(days=1),basis='hour',work_price=100,idle_price=80,created_by=self.leader)
        # A historical entry before any tariff is an explicit unpriced state.
        row.days.create(date=row.placement.start_date,hours=10,kind='work',amount=0,updated_by=self.worker)
        card=summary(scope);self.assertEqual(card['rental_unpriced'],1);self.assertIsNone(card['result'])
        self.client.force_login(self.leader);response=self.client.get(reverse('finance_detail',args=[scope.pk]));self.assertContains(response,'Затраты пока неполные')
