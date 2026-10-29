import uuid
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import ValidationError,PermissionDenied
from projects import test_stock as base,stock_planning
from projects.models import StockItem,StockBalance
from accounts.business_notices import unread
from .models import Report,CouplingUse
from .services import report_work,confirm_work
from .review_services import decide_problem

class ProblemReviewTests(TestCase):
    setUpTestData=classmethod(base.StockTests.setUpTestData.__func__)
    make_job=base.StockTests.make_job;wh=base.StockTests.wh;data=base.StockTests.data
    def setUp(self):
        self.job=self.make_job()
        self.problem=report_work(self.worker,self.job.pk,'unrepaired',{'token':uuid.uuid4(),'expected_version':self.job.version,'performed_date':self.today,'problem':'access','note':'Находится под железной дорогой','couplings':[],'files':[]})
        self.job.refresh_from_db();self.client.force_login(self.leader)
        self.url=reverse('repair_problem_review',args=[self.job.pk])
    def decide(self,decision='unrepairable'):
        return decide_problem(self.leader,self.job.pk,self.job.version,decision,'Находится под железной дорогой. Физически устранить нельзя.')
    def test_leader_accepts_impossibility_preserving_problem_and_history(self):
        self.assertTrue(unread(self.leader).exists())
        self.decide();self.job.refresh_from_db();self.problem.refresh_from_db()
        self.assertEqual(self.job.state,'unrepairable');self.assertEqual(self.job.defect_status,'Не подлежит устранению')
        self.assertEqual(self.problem.confirmed_by,self.leader);self.assertEqual(self.problem.note,'Находится под железной дорогой')
        self.assertTrue(Report.objects.filter(job=self.job,kind='problem_review').exists())
        self.assertFalse(unread(self.leader).exists())
        response=self.client.get(reverse('repairs')+'?state=unrepairable');self.assertContains(response,'Не подлежит устранению')
        self.assertNotContains(response,'data-review-job=');self.assertNotContains(response,'Внести результат')
    def test_acknowledging_temporary_obstacle_does_not_claim_repair_completed(self):
        self.decide('acknowledge');self.job.refresh_from_db()
        self.assertEqual(self.job.state,'progress');self.assertEqual(self.job.problem,'access')
        self.assertFalse(unread(self.leader).exists());self.assertEqual(self.job.defect_status,'Не устранён')
    def test_worker_cannot_accept_or_reopen_decision(self):
        with self.assertRaises(PermissionDenied):decide_problem(self.worker,self.job.pk,self.job.version,'unrepairable','Причина')
        self.client.force_login(self.worker);self.assertEqual(self.client.get(self.url).status_code,403)
    def test_stale_or_repeated_decision_does_not_add_history(self):
        version=self.job.version;self.decide()
        with self.assertRaises(ValidationError):decide_problem(self.leader,self.job.pk,version,'unrepairable','Причина')
        self.assertEqual(Report.objects.filter(job=self.job,kind='problem_review').count(),1)
    def test_uninstalled_reservations_are_freed_without_consuming_or_removing_stock(self):
        warehouse=self.wh();stock_planning.receive(self.leader,self.data(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8),warehouse,1,self.today,'База',uuid.uuid4())
        item=StockItem.objects.get();stock_planning.reserve(self.leader,item.pk,item.version,self.job.pk)
        self.decide();item.refresh_from_db()
        self.assertIsNone(item.reserved_job_id);self.assertEqual(StockBalance.objects.get(item=item).quantity,1)
        self.assertFalse(CouplingUse.objects.exists())
    def test_closed_impossibility_cannot_be_reported_complete_without_leader_reopening(self):
        self.decide();self.job.refresh_from_db()
        with self.assertRaises(ValidationError):report_work(self.worker,self.job.pk,'start',{'token':uuid.uuid4(),'expected_version':self.job.version,'performed_date':self.today})
        decide_problem(self.leader,self.job.pk,self.job.version,'reopen','Появилось техническое решение и согласованный доступ.')
        self.job.refresh_from_db();self.assertEqual(self.job.state,'progress');self.assertEqual(self.job.problem,'')
        self.assertTrue(Report.objects.filter(job=self.job,kind='reopen').exists())
    def test_review_form_is_reachable_and_saves_decision(self):
        self.assertContains(self.client.get(reverse('repair_detail',args=[self.job.pk])),'Не подлежит устранению')
        response=self.client.post(self.url,{'expected_version':self.job.version,'decision':'unrepairable','note':'Физически невозможно выполнить ремонт'})
        self.assertEqual(response.status_code,302);self.job.refresh_from_db();self.assertEqual(self.job.state,'unrepairable')

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.conf import settings
from projects.models import LegalEntity,COMPANY_TAX_ID
import os,json,subprocess
class ProblemReviewBrowser(StaticLiveServerTestCase):
    def test_completed_308265_is_green_notified_and_selectable_while_railway_problem_can_be_closed(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'Тест'})
        base.StockTests.setUpTestData.__func__(type(self))
        for user in (self.leader,self.worker):user.set_password('123456');user.save()
        completed=base.StockTests.make_job(self);blocked=base.StockTests.make_job(self)
        defect=completed.defects.get();defect.kilometre='308.265';defect.number='1757';defect.save()
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/problem-review-browser.cjs')],env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_REVIEW_FIXTURE':json.dumps({'completed':str(completed.pk),'blocked':str(blocked.pk)})},capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        completed.refresh_from_db();blocked.refresh_from_db()
        self.assertEqual((completed.state,blocked.state),('confirmed','unrepairable'))
