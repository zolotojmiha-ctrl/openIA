import io,os,json,subprocess,uuid
from decimal import Decimal
from django.test import TestCase
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.conf import settings
from django.urls import reverse
from django.core.exceptions import ValidationError,PermissionDenied
from django.core import signing
from accounts.models import AccessGrant
from projects import test_stock as base
from projects import stock_planning,stock_groups,stock
from projects.models import StockItem,StockBalance,LegalEntity,COMPANY_TAX_ID
from . import stock_assignment as service
from .models import CouplingUse,Defect,JobDefect

class DefectStockTests(TestCase):
    setUpTestData=classmethod(base.StockTests.setUpTestData.__func__)
    wh=base.StockTests.wh;data=base.StockTests.data;make_job=base.StockTests.make_job;report=base.StockTests.report
    def setUp(self):
        self.job=self.make_job();self.a=self.wh('Володя Барабинск');self.b=self.wh('Омск')
        self.client.force_login(self.leader)
        self.url=reverse('repair_stock_choose',args=[self.job.pk])
    def receive(self,wh=None,n=1,length=1500,wall=10,method='p2',serial=''):
        stock_planning.receive(self.leader,self.data(name='Муфта',kind='coupling',unit='pcs',method=method,diameter_mm=720,length_mm=length,wall_mm=wall,serial=serial),wh or self.a,n,self.today,'База',uuid.uuid4())
    def choice(self,changes):
        response=self.client.get(self.url);form=response.context['form']
        data={name:form[name].value() for name in form.fields}
        for row in response.context['rows']:
            group=row['group'];data['quantity_'+group['id']]=str(changes.get(group['warehouse'].name,row['own']))
        return data
    def held(self):return StockItem.objects.filter(reserved_job=self.job)
    def test_register_has_direct_action_and_shows_actual_dimensions(self):
        self.receive();response=self.client.get(reverse('repairs'))
        self.assertContains(response,'Муфты со склада');self.assertContains(response,self.url)
        self.assertEqual(self.client.post(self.url,self.choice({self.a.name:1})).status_code,302)
        response=self.client.get(reverse('repairs'));self.assertContains(response,'1500 мм · стенка 10');self.assertContains(response,self.a.name)
        self.assertContains(response,'Изменить муфты');self.assertEqual(self.held().count(),1)
    def test_conflicting_length_and_composition_explains_card_data_and_blocks_reservation(self):
        for method in ('p1','p2'):
            with self.subTest(method=method):
                self.job.method=method;self.job.length_m=Decimal('1');self.job.composition='1,5';self.job.save()
                self.receive(method=method)
                response=self.client.get(self.url)
                self.assertContains(response,'длина 1 м, а сумма состава 1,5 м')
                self.assertContains(response,reverse('repair_edit',args=[self.job.pk]))
                rows=[r for r in response.context['rows'] if r['group']['item'].method==method]
                self.assertTrue(rows)
                for row in rows:self.assertIn('сумма состава 1,5 м',row['reason'])
                item=StockItem.objects.filter(method=method).first()
                with self.assertRaisesMessage(ValidationError,'сумма состава 1,5 м'):
                    stock_planning.reserve(self.leader,item.pk,item.version,self.job.pk)
                self.assertFalse(self.held().exists())
                self.job.composition='';self.job.save()
                row=next(r for r in service.candidates(self.leader,self.job) if r['group']['item'].method==method)
                self.assertEqual(row['reason'],'')
                item=stock_planning.reserve(self.leader,item.pk,item.version,self.job.pk)
                self.assertEqual(item.length_mm,1500)
                stock_planning.reserve(self.leader,item.pk,item.version,None)

    def test_reservation_does_not_consume_stock_and_retry_does_not_duplicate(self):
        self.receive(n=3);data=self.choice({self.a.name:1})
        for _ in range(2):self.assertEqual(self.client.post(self.url,data).status_code,302)
        self.assertEqual(self.held().count(),1)
        self.assertEqual(sum(StockBalance.objects.values_list('quantity',flat=True)),3)
    def test_replace_across_warehouses_releases_old_before_reserving_new(self):
        self.receive();self.receive(self.b,length=1000,wall=8)
        self.client.post(self.url,self.choice({self.a.name:1}))
        response=self.client.post(self.url,self.choice({self.a.name:0,self.b.name:1}))
        self.assertEqual(response.status_code,302,response.context and response.context['form'].errors)
        self.assertEqual(self.held().get().balances.get(quantity__gt=0).warehouse,self.b)
    def test_unreserve_and_preserve_other_jobs_reservation(self):
        self.receive(n=3);other=self.make_job();item=StockItem.objects.first()
        stock_planning.reserve(self.leader,item.pk,item.version,other.pk)
        self.client.post(self.url,self.choice({self.a.name:1}))
        response=self.client.post(self.url,self.choice({self.a.name:0}))
        self.assertEqual(response.status_code,302);self.assertFalse(self.held().exists())
        self.assertEqual(StockItem.objects.filter(reserved_job=other).count(),1)
    def test_wrong_type_or_smaller_wall_is_rejected_without_losing_old_choice(self):
        self.receive();self.receive(self.b,wall=6)
        self.client.post(self.url,self.choice({self.a.name:1}))
        response=self.client.post(self.url,self.choice({self.a.name:0,self.b.name:1}))
        self.assertIn(response.status_code,(400,409))
        self.assertEqual(self.held().get().balances.get(quantity__gt=0).warehouse,self.a)
    def test_overbooking_several_warehouses_rolls_back_all(self):
        self.receive();self.receive(self.b)
        response=self.client.post(self.url,self.choice({self.a.name:1,self.b.name:1}))
        self.assertEqual(response.status_code,409);self.assertFalse(self.held().exists())
    def test_multiple_parts_and_shared_defects_use_one_reservation_set(self):
        self.job.length_m=Decimal('2.5');self.job.composition='1+1.5';self.job.save()
        other=Defect.objects.create(contract=self.contract,section=self.section,kilometre=1,number='ВТОРОЙ');JobDefect.objects.create(job=self.job,defect=other)
        self.receive(n=2)
        self.assertEqual(self.client.post(self.url,self.choice({self.a.name:2})).status_code,302)
        self.assertEqual(self.held().count(),2)
        self.assertContains(self.client.get(reverse('repairs')),'Общий комплект для 2 дефектов')
    def test_stale_stock_or_changed_job_does_not_overwrite(self):
        self.receive();data=self.choice({self.a.name:1});item=StockItem.objects.first()
        stock_planning.reserve(self.leader,item.pk,item.version,self.make_job().pk)
        response=self.client.post(self.url,data);self.assertEqual(response.status_code,409);self.assertFalse(self.held().exists())
    def test_worker_sees_only_own_assigned_material_and_cannot_change_stock(self):
        self.receive(serial='ЗАВ-123');self.client.post(self.url,self.choice({self.a.name:1}))
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(self.url).status_code,403)
        response=self.client.get(reverse('repairs'));self.assertContains(response,'ЗАВ-123');self.assertNotContains(response,'Выбрать со склада')
    def test_completed_work_is_readonly_and_installed_dimensions_remain_visible(self):
        self.receive(serial='МОНТАЖ-123');self.client.post(self.url,self.choice({self.a.name:1}))
        items=list(self.held().select_related('coupling'));self.report(self.job,[i.coupling for i in items])
        response=self.client.get(reverse('repairs'));self.assertContains(response,'Смонтировано');self.assertContains(response,'МОНТАЖ-123');self.assertNotContains(response,self.url)
        response=self.client.post(self.url,self.choice({self.a.name:0}));self.assertEqual(response.status_code,409)
        self.assertTrue(CouplingUse.objects.filter(job=self.job).exists())
    def test_return_context_and_empty_review_filter(self):
        self.receive();data=self.choice({self.a.name:1});back=reverse('repairs')+'?q=1&page=2';data['return_to']=back
        self.assertEqual(self.client.post(self.url,data).url,back)
        self.assertContains(self.client.get(reverse('repairs')+'?state=reported'),'По выбранному фильтру дефектов нет')

class DefectStockBrowser(StaticLiveServerTestCase):
    def test_choose_from_defect_then_replace_and_remove_in_real_browser(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'Тест'})
        base.StockTests.setUpTestData.__func__(type(self))
        self.leader.set_password('123456');self.leader.save()
        fixture=DefectStockTests();fixture.leader=self.leader;fixture.worker=self.worker;fixture.company=self.company;fixture.contract=self.contract;fixture.obj=self.obj;fixture.section=self.section;fixture.today=self.today
        fixture.job=fixture.make_job();fixture.a=fixture.wh('Володя Барабинск');fixture.b=fixture.wh('Омск');fixture.receive();fixture.receive(fixture.b,length=1000,wall=8)
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/defect-stock-browser.cjs')],env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_STOCK_JOB':str(fixture.job.pk)},capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse(StockItem.objects.filter(reserved_job=fixture.job).exists())
