import uuid
import os,json,subprocess
from decimal import Decimal
from django.test import TestCase
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from projects import test_stock as base
from projects import stock_planning as planning
from projects.models import StockItem
from accounts.models import AuditEvent
from accounts.business_notices import destination,notice_title,events
from .models import Coupling,Report
from .forms import ReportForm


class InstallationInputTests(TestCase):
    setUpTestData=classmethod(base.StockTests.setUpTestData.__func__)
    wh=base.StockTests.wh;data=base.StockTests.data;make_job=base.StockTests.make_job;balance=base.StockTests.balance

    def setUp(self):
        self.job=self.make_job();self.client.force_login(self.worker)
        self.url=reverse('repair_report',args=[self.job.pk,'repaired'])

    def payload(self,**changes):
        return {'token':str(uuid.uuid4()),'expected_version':self.job.version,
                'performed_date':str(self.today),'coordinates':'55°20\'22.2"N 77°49\'47.4"E',
                'length_m':'1.5','composition':'','manual_serials':'МОНТАЖ-1',**changes}

    def reserve(self):
        warehouse=self.wh()
        planning.receive(self.leader,self.data(kind='coupling',unit='pcs',method='p2',
            length_mm=1500,wall_mm=10),warehouse,1,self.today,'Поступление',uuid.uuid4())
        item=StockItem.objects.get()
        return warehouse,planning.reserve(self.leader,item.pk,item.version,self.job.pk)

    def test_manual_actual_length_serial_and_retry(self):
        payload=self.payload()
        for _ in range(2):
            response=self.client.post(self.url,payload)
            self.assertEqual(response.status_code,302,getattr(response,'context',None) and response.context['form'].errors)
        report=Report.objects.get();coupling=Coupling.objects.get()
        self.assertEqual((report.length_m,coupling.length_mm,coupling.serial),(Decimal('1.5'),1500,'МОНТАЖ-1'))
        self.assertEqual(report.latitude,Decimal('55.339500'))
        self.assertEqual(report.longitude,Decimal('77.829833'))
        self.job.refresh_from_db();self.assertEqual(self.job.length_m,1)
        self.assertFalse(StockItem.objects.exists())

    def test_manual_requires_serial(self):
        response=self.client.post(self.url,self.payload(manual_serials=''))
        self.assertIn('manual_serials',response.context['form'].errors)
        self.assertFalse(Report.objects.exists())

    def test_manual_multiple_parts_have_individual_serials(self):
        response=self.client.post(self.url,self.payload(composition='1+0.5',manual_serials='ПЕРВАЯ\nВТОРАЯ'))
        self.assertEqual(response.status_code,302,response.context and response.context['form'].errors)
        self.assertEqual(set(Coupling.objects.values_list('serial','length_mm')),{('ПЕРВАЯ',1000),('ВТОРАЯ',500)})

    def test_reserved_dimensions_are_read_only_serial_is_required_and_saved(self):
        warehouse,item=self.reserve()
        form=self.client.get(self.url).context['form']
        self.assertNotIn('length_m',form.fields)
        field='serial_'+item.coupling_id.hex
        data=self.payload();data['stock_prepared']=form.initial['stock_prepared'];data[field]=''
        response=self.client.post(self.url,data)
        self.assertIn(field,response.context['form'].errors)
        data[field]='ЗАВ-991';data['length_m']='999'
        for _ in range(2):
            response=self.client.post(self.url,data)
            self.assertEqual(response.status_code,302,response.context and response.context['form'].errors)
        item.refresh_from_db()
        self.assertEqual((item.serial,item.coupling.serial,item.length_mm),('ЗАВ-991','ЗАВ-991',1500))
        self.assertEqual(self.balance(warehouse,item),0)
        self.assertEqual(Report.objects.get().length_m,Decimal('1.5'))

    def test_success_returns_to_original_list_with_filters(self):
        back=reverse('repairs')+'?year=2026&page=2&master='+str(self.worker.pk)
        response=self.client.post(self.url,self.payload(return_to=back),HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(response.json()['url'],back)

    def test_external_return_address_is_not_used(self):
        response=self.client.post(self.url,self.payload(return_to='https://example.invalid/'),HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(response.json()['url'],reverse('work_place',args=[self.contract.pk,self.obj.pk]))

    def test_dms_ranges_and_hemispheres(self):
        for text,valid in [('55°60\'00"N 77°00\'00"E',False),('91°00\'00"N 77°00\'00"E',False),('55°20\'22.2"S 77°49\'47.4"W',True)]:
            form=ReportForm(data=self.payload(coordinates=text),job=self.job,action='repaired',user=self.worker)
            self.assertEqual(form.is_valid(),valid,form.errors)
            if valid:self.assertLess(form.cleaned_data['latitude'],0);self.assertLess(form.cleaned_data['longitude'],0)

    def test_notification_links_to_actual_job_and_distinguishes_problem(self):
        problem=AuditEvent.objects.create(actor=self.worker,action='repair_reported',details={'kind':'problem','entity_id':str(self.job.pk)})
        result=AuditEvent.objects.create(actor=self.worker,action='repair_reported',details={'kind':'complete','entity_id':str(self.job.pk)})
        note=AuditEvent.objects.create(actor=self.worker,action='repair_reported',details={'kind':'note','entity_id':str(self.job.pk)})
        self.assertEqual(destination(problem),reverse('repair_detail',args=[self.job.pk]))
        self.assertIn('Препятствие',notice_title(problem));self.assertIn('на проверке',notice_title(result))
        self.assertNotIn(note,events(self.leader))

    def test_pending_rows_have_disabled_choices_then_submitted_result_can_be_selected(self):
        self.client.force_login(self.leader)
        response=self.client.get(reverse('repairs'))
        self.assertContains(response,'результат ещё не передан на проверку')
        self.assertNotContains(response,'data-review-job=')
        self.client.force_login(self.worker);self.client.post(self.url,self.payload())
        self.client.force_login(self.leader)
        self.assertContains(self.client.get(reverse('repairs')),'data-review-job=')


    def test_reserved_number_conflict_does_not_change_stock_or_save_report(self):
        warehouse,item=self.reserve()
        planning.receive(self.leader,self.data(kind='coupling',unit='pcs',method='p2',
            length_mm=1000,wall_mm=8,serial='ЗАНЯТЫЙ'),warehouse,1,self.today,'Поступление',uuid.uuid4())
        form=self.client.get(self.url).context['form']
        data=self.payload(stock_prepared=form.initial['stock_prepared'])
        data['serial_'+item.coupling_id.hex]='ЗАНЯТЫЙ'
        response=self.client.post(self.url,data)
        self.assertEqual(response.status_code,409)
        item.refresh_from_db()
        self.assertEqual((item.serial,item.coupling.serial,self.balance(warehouse,item)),('','',1))
        self.assertFalse(Report.objects.exists())

    def test_retry_with_another_factory_number_does_not_overwrite_received_result(self):
        data=self.payload()
        self.assertEqual(self.client.post(self.url,data).status_code,302)
        data['manual_serials']='ДРУГОЙ НОМЕР'
        self.assertEqual(self.client.post(self.url,data).status_code,409)
        self.assertEqual(Coupling.objects.get().serial,'МОНТАЖ-1')
        self.assertEqual(Report.objects.count(),1)

    def test_advanced_other_method_does_not_require_a_coupling_serial(self):
        data=self.payload(method='grind',outcome='complete',manual_serials='')
        form=ReportForm(data=data,job=self.job,action='result',user=self.worker)
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.cleaned_data['method'],'grind')
        self.assertNotIn('installation',form.cleaned_data)
        response=self.client.post(reverse('repair_report',args=[self.job.pk,'result']),data|{'outcome':'partial'},follow=True)
        self.job.refresh_from_db();self.assertEqual(self.job.state,'partial')
        self.assertContains(response,'Сведения сохранены.')
        self.assertNotContains(response,'Он отмечен зелёным в ведомости.')


class InstallationInputBrowser(StaticLiveServerTestCase):
    def test_stock_and_manual_report_then_notification_and_bulk_review(self):
        from projects.models import LegalEntity,COMPANY_TAX_ID
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'Тестовая компания'})
        base.StockTests.setUpTestData.__func__(type(self))
        for user in (self.leader,self.worker):user.set_password('123456');user.save()
        job=base.StockTests.make_job(self);other=base.StockTests.make_job(self)
        warehouse=base.StockTests.wh(self,'Володя Барабинск')
        planning.receive(self.leader,base.StockTests.data(self,kind='coupling',unit='pcs',method='p2',length_mm=1500,wall_mm=10),warehouse,1,self.today,'Поставка',uuid.uuid4())
        item=StockItem.objects.get();item=planning.reserve(self.leader,item.pk,item.version,job.pk)
        fixture={'reserved':str(job.pk),'manual':str(other.pk),'coupling':str(item.coupling_id)}
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/installation-input-browser.cjs')],env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_INSTALLATION':json.dumps(fixture)},capture_output=True,text=True,timeout=110)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        item.refresh_from_db();job.refresh_from_db()
        self.assertEqual(item.serial,'ЗАВ-СКЛАД-001');self.assertEqual(job.state,'confirmed')
        self.assertEqual(other.reports.get().length_m,Decimal('1.5'))

class SubmissionFeedbackBrowser(StaticLiveServerTestCase):
    def run_submission(self,mode):
        from projects.models import LegalEntity,COMPANY_TAX_ID
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'Тестовая компания'})
        base.StockTests.setUpTestData.__func__(type(self))
        self.worker.set_password('123456');self.worker.save()
        job=base.StockTests.make_job(self)
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/submission-feedback-browser.cjs')],env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_REPORT_JOB':str(job.pk),'ARMADA_DRAFT_FAILURE':mode},capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        job.refresh_from_db();self.assertEqual(job.state,'reported');self.assertEqual(job.reports.count(),1)

    def test_missing_fields_and_stalled_draft_write(self):
        self.run_submission('write')

    def test_stalled_draft_open(self):
        self.run_submission('open')

    def test_stalled_draft_read(self):
        self.run_submission('read')

    def test_denied_draft_storage(self):
        self.run_submission('denied')
