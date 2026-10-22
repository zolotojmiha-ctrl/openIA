import secrets,uuid
from datetime import timedelta
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import ValidationError,PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.db.models import Sum
from accounts.models import User,AccessGrant,AuditEvent
from documents.models import Person,DocumentAccess
from repairs.models import Coupling,CouplingUse,Job,Section,Defect,JobDefect,Report
from repairs.services import report_work
from repairs.excel import workbook
from .models import LegalEntity,ProjectObject,ObjectAccess,Contract,ContractObject,ContractAccess,WarehouseAccess,StockBalance,StockMovement,StockEntry,StockItem,Warehouse,COMPANY_TAX_ID
from . import stock,stock_excel
from .crews import create_crew,assign_crew_place

class StockTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        password=secrets.token_urlsafe(24)
        cls.leader=User.objects.create_user('+70000000001',password,leader_slot=1,full_name='ТЕСТ Руководитель',must_change_password=False)
        cls.worker=User.objects.create_user('+70000000002',password,role='foreman',full_name='ТЕСТ Мастер',must_change_password=False)
        cls.supply=User.objects.create_user('+70000000003',password,role='supply',full_name='ТЕСТ Кладовщик',must_change_password=False)
        cls.company=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID);cls.obj=ProjectObject.objects.create(legal_entity=cls.company,name='ТЕСТ Омск–Татарск')
        cls.contract=Contract.objects.create(legal_entity=cls.company,number='ТЕСТ',subject='Дефекты',work_kinds=['repairs']);ContractObject.objects.create(contract=cls.contract,project_object=cls.obj)
        ObjectAccess.objects.create(user=cls.worker,project_object=cls.obj,granted_by=cls.leader);ContractAccess.objects.create(user=cls.worker,contract=cls.contract,granted_by=cls.leader)
        cls.section=Section.objects.create(project_object=cls.obj,rnu='ТЕСТ',pipeline='ТЕСТ',name='ТЕСТ')
        cls.today=timezone.localdate()
    def wh(self,name='ТЕСТ Основной',**changes):return stock.save_warehouse(self.leader,None,None,{'name':name,'location':'ТЕСТ адрес','project_object':None,'brigade_assignment':None,'archived':False,**changes})
    def data(self,**changes):return {'sku':'0001','name':'ТЕСТ Материал','kind':'material','unit':'kg','method':'','diameter_mm':None,'length_mm':None,'wall_mm':None,'coupling':None,'archived':False,**changes}
    def item(self,**changes):return stock.save_item(self.leader,None,None,self.data(**changes))
    def balance(self,wh,item):return StockBalance.objects.filter(warehouse=wh,item=item).aggregate(q=Sum('quantity'))['q'] or Decimal(0)
    def move(self,item,kind='receipt',q=10,src=None,dst=None,token=None,job=None,actor=None):return stock.move(actor or self.leader,item.pk,kind,q,src.pk if src else None,dst.pk if dst else None,self.today,'ТЕСТ основание',token or uuid.uuid4(),job.pk if job else None)
    def make_job(self):
        j=Job.objects.create(contract=self.contract,section=self.section,method='p2',length_m=1,wall_mm=8,assigned_to=self.worker,created_by=self.leader)
        d=Defect.objects.create(contract=self.contract,section=self.section,kilometre=1,number=str(Job.objects.count()));JobDefect.objects.create(job=j,defect=d);return j
    def report(self,j,couplings,action='result',token=None):return report_work(self.worker if action=='result' else self.leader,j.pk,action,{'token':token or uuid.uuid4(),'expected_version':j.version,'performed_date':self.today,'outcome':'complete','method':'p2','length_m':1,'composition':'','note':'ТЕСТ результат','problem':'','latitude':Decimal(55),'longitude':Decimal(73),'couplings':couplings,'files':[]})
    def concrete(self):
        c=Coupling.objects.create(project_object=self.obj,method='p2',length_mm=1000,wall_mm=8,serial='ТЕСТ-001',warehouse='Старое место')
        return c,self.item(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8,coupling=c)
    def crew(self):
        p=Person.objects.create(legal_entity=self.company,full_name='ТЕСТ Мастер',user=self.worker,created_by=self.leader)
        b=create_crew(self.leader,'ТЕСТ Бригада',p,[],self.today);return assign_crew_place(self.leader,b.pk,b.version,self.contract.pk,self.obj.pk,self.today,None)
    def test_role_does_not_grant_stock_management(self):
        self.client.force_login(self.supply);self.assertEqual(self.client.get(reverse('stock_new')).status_code,403)
        with self.assertRaises(PermissionDenied):stock.save_warehouse(self.supply,None,None,{'name':'X'})
        AccessGrant.objects.create(user=self.supply,permission='stock.manage',granted_by=self.leader);self.assertEqual(self.client.get(reverse('stock_new')).status_code,200)
    def test_receipt_transfer_and_ledger_totals(self):
        a=self.wh();b=self.wh('ТЕСТ Второй');i=self.item();self.move(i,dst=a);self.move(i,'transfer',3,a,b)
        self.assertEqual(self.balance(a,i),7);self.assertEqual(self.balance(b,i),3)
        for wh in (a,b):self.assertEqual(StockEntry.objects.filter(warehouse=wh,movement__item=i).aggregate(q=Sum('quantity'))['q'],self.balance(wh,i))
    def test_insufficient_balance_rolls_back_both_legs(self):
        a=self.wh();b=self.wh('Б');i=self.item();self.move(i,q=2,dst=a)
        with self.assertRaises(ValidationError):self.move(i,'transfer',3,a,b)
        self.assertEqual(self.balance(a,i),2);self.assertEqual(self.balance(b,i),0);self.assertEqual(StockMovement.objects.count(),1)
    def test_idempotent_operation_and_changed_payload(self):
        a=self.wh();i=self.item();token=uuid.uuid4();first=self.move(i,dst=a,token=token);second=self.move(i,dst=a,token=token);self.assertEqual(first.pk,second.pk);self.assertEqual(self.balance(a,i),10)
        with self.assertRaises(ValidationError):self.move(i,q=11,dst=a,token=token)
    def test_fractional_and_whole_units(self):
        a=self.wh();i=self.item();self.move(i,q='1.125',dst=a)
        for q in ('1.0001','NaN','Infinity',0,-1):
            with self.assertRaises(ValidationError):self.move(i,q=q,dst=a)
        j=self.item(sku='0002',unit='pcs')
        with self.assertRaises(ValidationError):self.move(j,q='1.5',dst=a)
    def test_read_access_does_not_grant_mutation_objects_or_files(self):
        wh=self.wh(project_object=self.obj);i=self.item();self.move(i,dst=wh)
        self.client.force_login(self.worker);self.assertEqual(self.client.get(reverse('stock_detail',args=[wh.pk])).status_code,404)
        WarehouseAccess.objects.create(warehouse=wh,user=self.worker,granted_by=self.leader)
        self.assertEqual(self.client.get(reverse('stock_detail',args=[wh.pk])).status_code,200);self.assertEqual(self.client.get(reverse('stock_move')).status_code,403)
        self.assertEqual(ObjectAccess.objects.filter(user=self.worker).count(),1);self.assertFalse(DocumentAccess.objects.exists())
    def test_hidden_warehouse_not_disclosed_by_transfer_history(self):
        a=self.wh();b=self.wh('СЕКРЕТНЫЙ СКЛАД');i=self.item();self.move(i,dst=a);self.move(i,'transfer',2,a,b);WarehouseAccess.objects.create(warehouse=a,user=self.worker,granted_by=self.leader)
        self.client.force_login(self.worker);self.assertNotContains(self.client.get(reverse('stock_detail',args=[a.pk])),'СЕКРЕТНЫЙ СКЛАД')
    def test_reversal_is_append_only_and_retry_safe(self):
        a=self.wh();i=self.item();r=self.move(i,dst=a);token=uuid.uuid4();back=stock.reverse(self.leader,r.pk,'Ошибка количества',token)
        self.assertEqual(stock.reverse(self.leader,r.pk,'Ошибка количества',token).pk,back.pk);self.assertEqual(self.balance(a,i),0);self.assertEqual(StockMovement.objects.count(),2)
        with self.assertRaises(ValueError):r.note='Перезаписать';r.save()
    def test_cannot_reverse_used_receipt(self):
        a=self.wh();i=self.item();r=self.move(i,dst=a);self.move(i,'loss',8,a)
        with self.assertRaises(ValidationError):stock.reverse(self.leader,r.pk,'Ошибка',uuid.uuid4())
        self.assertEqual(self.balance(a,i),2)
    def test_stock_archiving_requires_zero_balance_and_stale_versions_fail(self):
        a=self.wh();i=self.item();self.move(i,dst=a)
        with self.assertRaises(ValidationError):stock.save_warehouse(self.leader,a.pk,a.version,{'name':a.name,'archived':True})
        with self.assertRaises(ValidationError):stock.save_item(self.leader,i.pk,i.version,self.data(archived=True))
        with self.assertRaises(ValidationError):stock.save_item(self.leader,i.pk,999,self.data())
    def test_unique_codes_and_sizes_do_not_rewrite_history(self):
        self.item()
        with self.assertRaises(ValidationError):self.item()
        a=self.wh();i=StockItem.objects.get();self.move(i,dst=a)
        with self.assertRaises(ValidationError):stock.save_item(self.leader,i.pk,i.version,self.data(unit='m'))
    def test_issue_return_and_consume_for_brigade(self):
        a=self.wh();crew=self.crew();b=self.wh('ТЕСТ У бригады',brigade_assignment=crew);i=self.item();j=self.make_job();self.move(i,dst=a)
        self.move(i,'issue',4,a,b);self.move(i,'consume',1,b,job=j);self.move(i,'return',2,b,a)
        self.assertEqual(self.balance(a,i),8);self.assertEqual(self.balance(b,i),1)
    def test_brigade_dates_are_checked(self):
        a=self.wh();crew=self.crew();crew.start_date=self.today+timedelta(days=1);crew.save();b=self.wh('У бригады',brigade_assignment=crew);i=self.item();self.move(i,dst=a)
        with self.assertRaises(ValidationError):self.move(i,'issue',1,a,b)
    def test_concrete_coupling_received_once_and_automatically_consumed(self):
        c,i=self.concrete();a=self.wh(project_object=self.obj);self.move(i,q=1,dst=a)
        with self.assertRaises(ValidationError):self.move(i,q=1,dst=a)
        j=self.make_job();token=uuid.uuid4();r=self.report(j,[c],token=token);self.assertEqual(self.balance(a,i),0);self.assertEqual(StockMovement.objects.filter(automatic=True,kind='consume').count(),1)
        self.report(j,[c],token=token);self.assertEqual(StockMovement.objects.filter(kind='consume').count(),1);self.assertTrue(CouplingUse.objects.filter(coupling=c).exists())
    def test_correcting_installation_restores_stock_without_erasing_history(self):
        c,i=self.concrete();a=self.wh(project_object=self.obj);self.move(i,q=1,dst=a);j=self.make_job();old=self.report(j,[c]);j.refresh_from_db();self.report(j,[],action='correction')
        self.assertEqual(self.balance(a,i),1);self.assertFalse(CouplingUse.objects.filter(coupling=c).exists());self.assertEqual(StockMovement.objects.filter(automatic=True).count(),2);self.assertEqual(Report.objects.get(pk=old.pk).coupling_snapshot[0]['id'],str(c.pk))
    def test_coupling_without_stock_rejects_report_atomically(self):
        c,i=self.concrete();j=self.make_job()
        with self.assertRaises(ValidationError):self.report(j,[c])
        self.assertFalse(j.reports.exists());self.assertFalse(CouplingUse.objects.exists());j.refresh_from_db();self.assertEqual(j.state,'planned')
    def test_legacy_couplings_do_not_invent_stock(self):
        c=Coupling.objects.create(project_object=self.obj,method='p2',length_mm=1000,wall_mm=8,serial='LEGACY',warehouse='Старый склад');j=self.make_job();self.report(j,[c]);self.assertFalse(StockMovement.objects.exists());self.assertFalse(StockBalance.objects.exists())
    def test_installed_couplings_cannot_be_moved_or_linked_as_new_stock(self):
        c,i=self.concrete();a=self.wh(project_object=self.obj);self.move(i,q=1,dst=a);j=self.make_job();self.report(j,[c]);b=self.wh('Б')
        with self.assertRaises(ValidationError):self.move(i,'transfer',1,a,b)
        with self.assertRaises(ValidationError):self.item(sku='0002',kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8,coupling=c)
    def test_manual_consumption_of_linked_coupling_is_rejected(self):
        c,i=self.concrete();a=self.wh();self.move(i,q=1,dst=a)
        with self.assertRaises(ValidationError):self.move(i,'consume',1,a,job=self.make_job())
    def book(self,kind,rows):return SimpleUploadedFile('stock.xlsx',workbook(rows,headers=stock_excel.HEADERS[kind]))
    def test_excel_roundtrips_warehouse_items_operations(self):
        a=self.wh();i=self.item();self.move(i,dst=a)
        for kind in ('warehouses','items','movements'):
            data=stock_excel.export_rows(self.leader,kind);rows=stock_excel.parse_rows(self.book(kind,data),kind,self.leader)
            with transaction.atomic():stock_excel.apply_rows(self.leader,kind,rows)
        self.assertEqual(self.balance(a,i),10);self.assertEqual(StockMovement.objects.count(),1)
    def test_excel_preview_and_apply_retry_do_not_duplicate_receipts(self):
        a=self.wh();i=self.item();url=reverse('stock_import',args=['movements']);self.client.force_login(self.leader)
        data=[['','receipt',str(i.pk),'12.500','',str(a.pk),str(self.today),'Начальный остаток','']]
        r=self.client.post(url,{'file':self.book('movements',data)});self.assertEqual(r.status_code,200);self.assertFalse(StockMovement.objects.exists());token=r.context['preview'];self.assertTrue(token)
        for _ in range(2):self.assertEqual(self.client.post(url,{'apply':'1','preview':token}).status_code,302)
        self.assertEqual(self.balance(a,i),Decimal('12.5'));self.assertEqual(StockMovement.objects.count(),1)
    def test_bad_later_import_row_rolls_back_earlier_receipt(self):
        a=self.wh();i=self.item();rows=[{'row':2,'token':str(uuid.uuid4()),'values':['','receipt',str(i.pk),'2','',str(a.pk),str(self.today),'Тест','']},{'row':3,'token':str(uuid.uuid4()),'values':['','loss',str(i.pk),'3',str(a.pk),'',str(self.today),'Ошибка','']}]
        with self.assertRaises(ValidationError):
            with transaction.atomic():stock_excel.apply_rows(self.leader,'movements',rows)
        self.assertFalse(StockMovement.objects.exists());self.assertEqual(self.balance(a,i),0)
    def test_balances_are_export_only(self):
        self.client.force_login(self.leader);self.assertEqual(self.client.get(reverse('stock_import',args=['balances'])).status_code,404);self.assertEqual(self.client.get(reverse('stock_export',args=['balances'])).status_code,200)

    def test_explicit_issue_to_job_prepares_simple_foreman_result(self):
        from repairs.forms import ReportForm
        c,i=self.concrete();a=self.wh();crew=self.crew();b=self.wh('ТЕСТ Выдано',brigade_assignment=crew);j=self.make_job();self.move(i,q=1,dst=a);self.move(i,'issue',1,a,b,job=j)
        snapshot=ReportForm(job=j,action='repaired',user=self.worker).initial['stock_prepared']
        form=ReportForm({'stock_prepared':snapshot,'token':str(uuid.uuid4()),'expected_version':j.version,'performed_date':str(self.today),'coordinates':'55.123456, 73.123456','note':''},job=j,action='repaired',user=self.worker)
        self.assertTrue(form.is_valid(),form.errors);self.assertEqual([x.pk for x in form.cleaned_data['couplings']],[c.pk])
        report_work(self.worker,j.pk,form.action,form.cleaned_data);self.assertEqual(self.balance(b,i),0);self.assertTrue(CouplingUse.objects.filter(coupling=c,job=j).exists())
    def test_returned_issue_invalidates_old_simple_form(self):
        from repairs.forms import ReportForm
        c,i=self.concrete();a=self.wh();crew=self.crew();b=self.wh('ТЕСТ Выдано',brigade_assignment=crew);j=self.make_job();self.move(i,q=1,dst=a);self.move(i,'issue',1,a,b,job=j)
        snapshot=ReportForm(job=j,action='repaired',user=self.worker).initial['stock_prepared']
        self.move(i,'return',1,b,a)
        form=ReportForm({'stock_prepared':snapshot,'token':str(uuid.uuid4()),'expected_version':j.version,'performed_date':str(self.today),'coordinates':'55.123456, 73.123456'},job=j,action='repaired',user=self.worker)
        self.assertFalse(form.is_valid());self.assertIn('Состав выданных муфт изменился',str(form.non_field_errors()));self.assertEqual(self.balance(a,i),1);self.assertFalse(j.reports.exists())
