import uuid
from decimal import Decimal
from django.test import TestCase,override_settings
from django.urls import reverse
from django.core.exceptions import ValidationError,PermissionDenied
from accounts.models import AuditEvent
from projects.models import StockItem,StockMovement
from projects import test_stock as fixtures,stock_planning
from . import stock_reconciliation as service
from .models import Coupling,CouplingUse,Report
from .test_installation_stock import InstallationStockTests


class StockReconciliationTests(TestCase):
    setUpTestData=classmethod(fixtures.StockTests.setUpTestData.__func__)
    wh=fixtures.StockTests.wh;data=fixtures.StockTests.data;make_job=fixtures.StockTests.make_job
    balance=fixtures.StockTests.balance;receive=InstallationStockTests.receive

    def setUp(self):
        self.job=self.make_job();self.job.method='p1';self.job.length_m=2;self.job.wall_mm=10;self.job.state='reported';self.job.save()
        self.warehouse=self.wh(project_object=self.obj)
        self.item=self.receive()[0]
        self.coupling=Coupling.objects.create(project_object=self.obj,method='p1',length_mm=2000,wall_mm=10,serial='ТЕСТ ФАКТ',source={'reported_without_stock':True})
        self.report=Report.objects.create(job=self.job,author=self.worker,kind='complete',performed_date=self.today,
            coupling_snapshot=[{'id':str(self.coupling.pk),'serial':self.coupling.serial,'method':'П1','length_mm':2000,'wall_mm':'10','warehouse':''}])
        CouplingUse.objects.create(job=self.job,coupling=self.coupling,report=self.report)

    def test_preview_is_read_only_confirm_adds_one_posting_preserves_report_and_repeat(self):
        snapshot=self.report.coupling_snapshot;version=self.job.version
        facts,token=service.preview(self.leader,self.job.pk,{})
        self.assertEqual(self.balance(self.warehouse,self.item),1)
        self.assertFalse(StockMovement.objects.filter(kind='consume').exists())
        service.confirm(self.leader,token);service.confirm(self.leader,token)
        self.item.refresh_from_db();self.report.refresh_from_db();self.job.refresh_from_db()
        self.assertEqual(self.balance(self.warehouse,self.item),0)
        self.assertEqual(self.item.coupling_id,self.coupling.pk)
        self.assertEqual(self.report.coupling_snapshot,snapshot)
        self.assertEqual(Report.objects.count(),1)
        self.assertEqual(self.job.state,'reported');self.assertEqual(self.job.version,version+1)
        self.assertEqual(StockMovement.objects.filter(kind='consume').count(),1)
        self.assertTrue(AuditEvent.objects.filter(action='repair_stock_reconciled').exists())

    def test_stale_preview_does_not_change_stock(self):
        facts,token=service.preview(self.leader,self.job.pk,{})
        self.item.version+=1;self.item.save()
        with self.assertRaises(ValidationError):service.confirm(self.leader,token)
        self.assertEqual(self.balance(self.warehouse,self.item),1)

    def test_foreman_cannot_reconcile_old_results(self):
        with self.assertRaises(PermissionDenied):service.preview(self.worker,self.job.pk,{})
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse('repair_stock_reconcile',args=[self.job.pk])).status_code,403)

    def test_cabinet_preview_confirmation_and_cancel_link(self):
        self.client.force_login(self.leader)
        url=reverse('repair_stock_reconcile',args=[self.job.pk])
        self.assertContains(self.client.get(url),'Проверить списание')
        response=self.client.post(url,{})
        self.assertContains(response,'Подтвердить списание')
        token=response.context['preview']
        response=self.client.post(url,{'stage':'confirm','preview':token})
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.balance(self.warehouse,self.item),0)

    @override_settings(ARMADA_MAX_ENABLED=True,ARMADA_MAX_PUBLIC_ORIGIN='https://example.invalid')
    def test_native_max_preview_confirm_and_duplicate_callback(self):
        from maxbot import tests as base
        from maxbot.models import Draft,Outbox,Inbox
        from maxbot import worker
        binding=base.MaxTests.bind(self,501,601,user=self.leader)
        def event(text):
            row=Inbox.objects.create(key=uuid.uuid4().hex,max_user=501,chat=601,payload={'kind':'message_created','text':text})
            worker.process(row.pk);row.refresh_from_db();self.assertEqual(row.state,'done')
            return row
        event('/сверка_муфт')
        event('stockfix:select:'+str(self.job.pk))
        draft=Draft.objects.get(action='stockfix')
        from .installation_stock import source_key
        event(f'stockfix:source:{draft.pk}:0:{source_key(self.item.pk,self.warehouse.pk)}')
        draft.refresh_from_db();self.assertEqual(draft.state,'confirm')
        self.assertEqual(self.balance(self.warehouse,self.item),1)
        event(f'stockfix:confirm:{draft.pk}');event(f'stockfix:confirm:{draft.pk}')
        draft.refresh_from_db();self.assertEqual(draft.state,'submitted')
        self.assertEqual(StockMovement.objects.filter(kind='consume').count(),1)
        self.assertTrue(Outbox.objects.filter(key__startswith='coupling-stock:').exists())

    @override_settings(ARMADA_MAX_ENABLED=True,ARMADA_MAX_PUBLIC_ORIGIN='https://example.invalid')
    def test_revoked_rights_after_preview_block_confirmation(self):
        facts,token=service.preview(self.leader,self.job.pk,{})
        self.leader.leader_slot=None;self.leader.role='foreman';self.leader.save()
        with self.assertRaises(PermissionDenied):service.confirm(self.leader,token)
        self.assertEqual(self.balance(self.warehouse,self.item),1)

    def test_two_legacy_installations_can_use_two_units_of_one_bulk_balance(self):
        from projects import stock
        from .installation_stock import source_key
        bulk=stock.save_item(self.leader,None,None,self.data(kind='coupling',unit='pcs',method='p1',diameter_mm=self.section.diameter_mm,length_mm=2000,wall_mm=10))
        stock.move(self.leader,bulk.pk,'receipt',2,None,self.warehouse.pk,self.today,'ТЕСТ',uuid.uuid4())
        other=Coupling.objects.create(project_object=self.obj,method='p1',length_mm=2000,wall_mm=10,serial='ТЕСТ ВТОРАЯ',source={'reported_without_stock':True})
        CouplingUse.objects.create(job=self.job,coupling=other,report=self.report)
        source=source_key(bulk.pk,self.warehouse.pk)
        _,token=service.preview(self.leader,self.job.pk,{str(c.pk):source for c in (self.coupling,other)})
        service.confirm(self.leader,token)
        self.assertEqual(StockMovement.objects.filter(kind='consume').count(),2)
        self.assertEqual(Report.objects.count(),1)
        self.assertEqual(self.balance(self.warehouse,self.item),1)


from django.contrib.staticfiles.testing import StaticLiveServerTestCase
class StockReconciliationBrowser(StaticLiveServerTestCase):
    wh=fixtures.StockTests.wh;data=fixtures.StockTests.data;make_job=fixtures.StockTests.make_job
    balance=fixtures.StockTests.balance;receive=InstallationStockTests.receive
    def test_mobile_owner_preview_and_confirmation(self):
        import os,subprocess
        from django.conf import settings
        from projects.models import LegalEntity,COMPANY_TAX_ID
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixtures.StockTests.setUpTestData.__func__(type(self))
        StockReconciliationTests.setUp(self)
        self.leader.set_password('123456');self.leader.save()
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/stock-reconciliation-browser.cjs')],
            env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_RECONCILE_JOB':str(self.job.pk)},
            capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(StockMovement.objects.filter(kind='consume').count(),1)
        self.assertEqual(Report.objects.count(),1)
