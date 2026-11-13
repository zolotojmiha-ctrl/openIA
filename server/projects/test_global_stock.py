"""Global physical selection and explicit, atomic approval of that selection."""
from unittest.mock import patch
from django.test import TestCase
from django.core.exceptions import ValidationError, PermissionDenied
from django.urls import reverse
from repairs.models import Defect, JobDefect
from . import test_stock as fixture, test_stock_oversize as sized
from . import stock_allocation, repair_materials, stock_planning, stock_optimizer
from .models import StockMovement, StockItem

class GlobalStockTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    wh=fixture.StockTests.wh;data=fixture.StockTests.data;make_job=fixture.StockTests.make_job
    balance=fixture.StockTests.balance
    setUp=sized.StockOversizeTests.setUp;job=sized.StockOversizeTests.job;piece=sized.StockOversizeTests.piece

    def records(self):return repair_materials.all_demand(self.leader)
    def token(self):
        records=self.records()
        return stock_allocation.proposal(self.leader,records,{r['job'].pk for r in records})[1]
    def missing(self):return sum(line['missing'] for row in self.records() for line in row['couplings'])

    def test_short_and_long_alternative_is_approved_without_changing_balances(self):
        short=self.job(length='1.5');long=self.job(length='3')
        for size in (1500,1500,1000,2000):self.piece(length=size,wall=8)
        before=list(StockMovement.objects.values_list('pk',flat=True))
        records=self.records();by_job={r['job'].pk:r for r in records}
        self.assertEqual([p['item'].length_mm for p in by_job[short.pk]['allocation']],[1500])
        self.assertEqual(sorted(p['item'].length_mm for p in by_job[long.pk]['allocation']),[1000,2000])
        self.assertEqual(self.missing(),0)
        token=self.token();self.assertEqual(StockItem.objects.filter(reserved_job__isnull=False).count(),0)
        self.assertEqual(stock_allocation.apply(self.leader,token),2)
        long.refresh_from_db();self.assertEqual(long.composition,'2+1')
        self.assertEqual(stock_allocation.apply(self.leader,token),2)
        self.assertEqual(StockItem.objects.filter(reserved_job__isnull=False).count(),3)
        self.assertEqual(list(StockMovement.objects.values_list('pk',flat=True)),before)
        self.assertEqual(self.missing(),0)

    def test_two_1500_close_3000_but_procurement_prefers_same_without_stock(self):
        job=self.job(length='3')
        self.assertEqual([(p['length'],p['missing']) for r in self.records() for p in r['couplings']],[(1500,2)])
        self.piece(length=1500);self.piece(length=1500)
        self.assertEqual(self.missing(),0);stock_allocation.apply(self.leader,self.token())
        job.refresh_from_db();self.assertEqual(job.composition,'1.5+1.5')

    def test_distinct_defects_outweigh_number_of_jobs(self):
        common=self.job(length='3')
        for number in ('TEST-A','TEST-B'):
            defect=Defect.objects.create(contract=self.contract,section=self.section,kilometre=100,number=number)
            JobDefect.objects.create(job=common,defect=defect)
        self.job(length='1.5');self.job(length='1.5')
        for _ in range(2):self.piece(length=1500)
        rows=self.records()
        self.assertEqual([r['job'].pk for r in rows if r['stock_complete']],[common.pk])

    def test_no_joins_for_p2_and_thin_or_other_diameter_is_not_used(self):
        self.job(method='p2',length='1.5',wall=10)
        self.piece(method='p2',length=1000,wall=10);self.piece(method='p2',length=1000,wall=10)
        self.piece(method='p2',length=1500,wall=8);self.piece(method='p2',length=1500,wall=10,diameter=820)
        self.assertEqual(self.missing(),1)

    def test_old_reservations_stay_and_completion_uses_same_wall(self):
        job=self.job(length='3',composition='2+1',wall=8)
        old=self.piece(length=2000,wall=10);old=stock_planning.reserve(self.leader,old.pk,old.version,job.pk)
        self.piece(length=1000,wall=8);new=self.piece(length=1000,wall=10)
        self.assertEqual(self.missing(),0)
        self.assertEqual(stock_allocation.apply(self.leader,self.token()),1)
        old.refresh_from_db();new.refresh_from_db()
        self.assertEqual(old.reserved_job_id,job.pk);self.assertEqual(new.reserved_job_id,job.pk)

    def test_stale_stock_or_job_rejects_whole_proposal(self):
        job=self.job(length='1.5');item=self.piece(length=1500);token=self.token()
        item.version+=1;item.save(update_fields=['version'])
        with self.assertRaises(ValidationError):stock_allocation.apply(self.leader,token)
        self.assertFalse(StockItem.objects.filter(reserved_job__isnull=False).exists())
        token=self.token();job.version+=1;job.save(update_fields=['version'])
        with self.assertRaises(ValidationError):stock_allocation.apply(self.leader,token)

    def test_approval_rolls_back_all_jobs_on_reservation_failure(self):
        self.job(length='1.5');self.job(length='1.5');self.piece(length=1500);self.piece(length=1500)
        original=stock_planning.reserve;calls=[]
        def reserve(*args):
            calls.append(1)
            if len(calls)==2:raise ValidationError('TEST rollback')
            return original(*args)
        with patch.object(stock_planning,'reserve',side_effect=reserve):
            with self.assertRaises(ValidationError):stock_allocation.apply(self.leader,self.token())
        self.assertFalse(StockItem.objects.filter(reserved_job__isnull=False).exists())

    def test_legacy_counted_stock_is_split_without_losing_total(self):
        from . import stock
        import uuid
        job=self.job(length='3')
        item=stock.save_item(self.leader,None,None,self.data(kind='coupling',unit='pcs',method='p1',diameter_mm=720,length_mm=1500,wall_mm=8))
        stock.move(self.leader,item.pk,'receipt',2,None,self.warehouse.pk,self.today,'TEST legacy',uuid.uuid4())
        self.assertEqual(stock_allocation.apply(self.leader,self.token()),1)
        self.assertEqual(StockItem.objects.filter(reserved_job=job).count(),2)
        from .models import StockBalance
        from django.db.models import Sum
        self.assertEqual(StockBalance.objects.aggregate(total=Sum('quantity'))['total'],2)

    def test_signature_actor_and_read_only_view(self):
        job=self.job(length='1.5');self.piece(length=1500)
        response=self.client.get(reverse('stock_coverage'))
        self.assertContains(response,'Закрепить показанный подбор')
        token=response.context['stock_plan_token']
        with self.assertRaises(ValidationError):stock_allocation.apply(self.leader,token+'x')
        with self.assertRaises(PermissionDenied):stock_allocation.apply(self.worker,token)
        self.assertEqual(self.client.get(reverse('stock_allocation_apply')).status_code,405)
        self.assertEqual(self.client.post(reverse('stock_allocation_apply'),{'proposal':token}).status_code,302)
        self.assertEqual(StockItem.objects.filter(reserved_job=job).count(),1)

    def test_unproved_optimizer_result_blocks_customer_and_max(self):
        self.job(length='3')
        with patch.object(stock_optimizer,'solve',side_effect=ValidationError(stock_optimizer.ERROR)):
            rows=self.records();self.assertTrue(rows[0]['allocation_failed'])
            from maxbot.demand_watch import snapshot
            with self.assertRaises(ValidationError):snapshot(self.leader)

class OptimizerOracleTests(TestCase):
    def test_completed_defects_equal_exhaustive_physical_assignment(self):
        import itertools,json,random
        randomizer=random.Random(1731)
        for trial in range(12):
            needs=[randomizer.choice((1000,1500,2500,3000)) for _ in range(3)]
            lengths=[randomizer.choice((1000,1500,2000)) for _ in range(4)]
            weights=[randomizer.choice((1,2,3)) for _ in range(3)]
            best=0
            for assignment in itertools.product(range(4),repeat=4):
                sums=[sum(length for length,destination in zip(lengths,assignment) if destination==i) for i in range(3)]
                best=max(best,sum(weight for i,weight in enumerate(weights) if needs[i]<=sums[i]<=10500))
            jobs=[{'id':str(i),'method':'p1','wall':'8','fixed':False,'defects':[f'{i}:{d}' for d in range(weights[i])],
                   'required':[[need,1]],'gap':need,'max':10500} for i,need in enumerate(needs)]
            groups=[{'method':'p1','length':length,'wall':'8','jobs':[0,1,2],'quantity':lengths.count(length)} for length in sorted(set(lengths))]
            answer=stock_optimizer.solve(json.dumps({'jobs':jobs,'groups':groups},sort_keys=True))
            self.assertEqual(sum(weights[i] for i,row in enumerate(answer) if row[3]),best,(needs,lengths,weights))

from unittest import skipUnless
from django.test import TransactionTestCase
from django.db import connection, close_old_connections, connections

@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL stock row locks')
class GlobalStockConcurrencyTests(TransactionTestCase):
    wh=fixture.StockTests.wh;data=fixture.StockTests.data;make_job=fixture.StockTests.make_job
    job=sized.StockOversizeTests.job;piece=sized.StockOversizeTests.piece
    def setUp(self):
        from .models import LegalEntity,COMPANY_TAX_ID
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        self.section.diameter_mm=720;self.section.save();self.warehouse=self.wh()
        self.job(length='1.5');self.piece(length=1500)
    def test_same_signed_proposal_concurrently_reserves_only_once(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        rows=repair_materials.all_demand(self.leader)
        token=stock_allocation.proposal(self.leader,rows,{row['job'].pk for row in rows})[1]
        barrier=threading.Barrier(2)
        def approve(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return stock_allocation.apply(self.leader,token)
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(list(pool.map(approve,[0,1])),[1,1])
        self.assertEqual(StockItem.objects.filter(reserved_job__isnull=False).count(),1)
        from accounts.models import AuditEvent
        self.assertEqual(AuditEvent.objects.filter(details__kind='global_allocation').count(),1)
