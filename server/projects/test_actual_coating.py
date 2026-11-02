import uuid
from decimal import Decimal
from django.test import TestCase
from django.core.exceptions import ValidationError
from . import test_p1_coating_and_undo as fixtures
from .p1_coating import coating
from .models import RepairConsumption, StockItem
from repairs.services import report_work
from repairs import plans


class ActualCoatingTests(TestCase):
    setUpTestData=classmethod(fixtures.P1CoatingAndUndoTests.setUpTestData.__func__)
    setUp=fixtures.P1CoatingAndUndoTests.setUp
    wh=fixtures.P1CoatingAndUndoTests.wh
    data=fixtures.P1CoatingAndUndoTests.data
    item=fixtures.P1CoatingAndUndoTests.item
    move=fixtures.P1CoatingAndUndoTests.move
    balance=fixtures.P1CoatingAndUndoTests.balance
    make_job=fixtures.P1CoatingAndUndoTests.make_job
    job=fixtures.P1CoatingAndUndoTests.job
    supplies=fixtures.P1CoatingAndUndoTests.supplies
    pieces=fixtures.P1CoatingAndUndoTests.pieces
    def test_extra_cleaning_adds_only_extra_pipe_area(self):
        job=self.job('1','')
        base=coating(job)
        job.actual_cleaning_length_m='2.4'
        actual=coating(job)
        self.assertEqual([x['quantity'] for x in actual],['1.575','38.034','5.932'])
        self.assertTrue(all(Decimal(x['extra_area_m2'])==Decimal('2.26080') for x in actual))
        job.actual_cleaning_length_m='1.2'
        self.assertEqual([x['quantity'] for x in coating(job)],[x['quantity'] for x in base])

    def test_fact_survives_posting_and_retry_does_not_double_consume(self):
        job=self.job('1','');wh,items=self.supplies();pieces=self.pieces(wh,(1000,))
        data={'token':uuid.uuid4(),'expected_version':job.version,'performed_date':self.today,
              'outcome':'complete','method':'p1','length_m':Decimal(1),'composition':'',
              'actual_cleaning_length_m':Decimal('2.4'),'note':'ТЕСТ','couplings':pieces,'files':[]}
        report=report_work(self.worker,job.pk,'result',data)
        self.assertEqual(report.context['actual_cleaning_length_m'],'2.4')
        self.assertEqual(RepairConsumption.objects.get(job=job).state,'posted')
        self.assertEqual(self.balance(wh,items['wrapper']),Decimal('94.068'))
        self.assertEqual(report_work(self.worker,job.pk,'result',data).pk,report.pk)
        self.assertEqual(self.balance(wh,items['wrapper']),Decimal('94.068'))
        job.refresh_from_db()
        data.update(token=uuid.uuid4(),expected_version=job.version,actual_cleaning_length_m=Decimal('2.400'))
        report_work(self.leader,job.pk,'correction',data)
        self.assertEqual(RepairConsumption.objects.get(job=job).state,'posted')
        self.assertEqual(self.balance(wh,items['wrapper']),Decimal('94.068'))
        job.refresh_from_db()
        data.update(token=uuid.uuid4(),expected_version=job.version,actual_cleaning_length_m=Decimal('3.4'))
        report_work(self.leader,job.pk,'correction',data)
        self.assertEqual(RepairConsumption.objects.get(job=job).state,'review')
        self.assertEqual(self.balance(wh,items['wrapper']),Decimal('94.068'))

    def test_plan_replaces_reservation_without_consuming_stock(self):
        job=self.make_job();wh=self.wh()
        old=self.item(sku='OLD',kind='coupling',unit='pcs',method='p2',diameter_mm=720,length_mm=1000,wall_mm=8)
        new=self.item(sku='NEW',kind='coupling',unit='pcs',method='p2',diameter_mm=720,length_mm=1500,wall_mm=8)
        self.move(old,dst=wh,q=1);self.move(new,dst=wh,q=1)
        from .stock_planning import reserve
        old=reserve(self.leader,old.pk,old.version,job.pk)
        job,released=plans.revise(self.leader,job.pk,job.version,dict(method='p2',length_m=Decimal('1.5'),wall_mm=Decimal(8),composition=''),'ДДК')
        old.refresh_from_db();new.refresh_from_db()
        self.assertIsNone(old.reserved_job_id);self.assertEqual(new.reserved_job_id,job.pk)
        self.assertEqual(self.balance(wh,old),1);self.assertEqual(self.balance(wh,new),1)
        self.assertEqual(len(released),1)

    def test_switch_to_p1_allocates_two_pieces_from_group(self):
        job=self.make_job();wh=self.wh()
        group=self.item(sku='P1-GROUP',kind='coupling',unit='pcs',method='p1',diameter_mm=720,length_mm=2000,wall_mm=8)
        self.move(group,dst=wh,q=3)
        plans.revise(self.leader,job.pk,job.version,dict(method='p1',length_m=Decimal(4),wall_mm=Decimal(8),composition='2+2'),'ДДК')
        self.assertEqual(StockItem.objects.filter(reserved_job=job).count(),2)
        self.assertEqual(sum(i.balances.get().quantity for i in StockItem.objects.filter(reserved_job=job)),2)

    def test_missing_replacement_releases_old_and_keeps_new_demand(self):
        job=self.make_job();wh=self.wh()
        old=self.item(kind='coupling',unit='pcs',method='p2',diameter_mm=720,length_mm=1000,wall_mm=8)
        self.move(old,dst=wh,q=1)
        from .stock_planning import reserve
        old=reserve(self.leader,old.pk,old.version,job.pk)
        plans.revise(self.leader,job.pk,job.version,dict(method='p2',length_m=Decimal('1.5'),wall_mm=Decimal(8),composition=''),'ДДК')
        old.refresh_from_db();job.refresh_from_db()
        self.assertIsNone(old.reserved_job_id)
        self.assertEqual(job.length_m,Decimal('1.5'))
        self.assertFalse(StockItem.objects.filter(reserved_job=job).exists())

    def test_form_offers_fact_only_for_supported_result(self):
        from repairs.forms import ReportForm
        job=self.job('1','')
        self.assertIn('actual_cleaning_length_m',ReportForm(job=job,action='repaired',user=self.worker).fields)
        self.assertNotIn('actual_cleaning_length_m',ReportForm(job=job,action='unrepaired',user=self.worker).fields)
        job.method='p2'
        self.assertNotIn('actual_cleaning_length_m',ReportForm(job=job,action='repaired',user=self.worker).fields)

    def test_pending_material_retry_uses_saved_fact(self):
        from . import repair_materials
        job=self.job('1','');wh,items=self.supplies(skip=('wrapper',));pieces=self.pieces(wh,(1000,))
        report_work(self.worker,job.pk,'result',{'token':uuid.uuid4(),'expected_version':job.version,
            'performed_date':self.today,'outcome':'complete','method':'p1','length_m':Decimal(1),
            'actual_cleaning_length_m':Decimal('2.4'),'note':'ТЕСТ','couplings':pieces,'files':[]})
        self.assertEqual(RepairConsumption.objects.get(job=job).state,'pending')
        self.assertEqual(self.balance(wh,items['primer']),100)
        wrapper=self.item(sku='LATER',kind='wrapper',unit='kg')
        self.move(wrapper,dst=wh,q=100)
        repair_materials.retry(self.leader,job)
        self.assertEqual(self.balance(wh,wrapper),Decimal('94.068'))

    def test_replacement_does_not_steal_reserved_piece(self):
        from .stock_planning import reserve
        job=self.make_job();other=self.make_job();other.length_m=Decimal('1.5');other.save()
        wh=self.wh()
        item=self.item(kind='coupling',unit='pcs',method='p2',diameter_mm=720,length_mm=1500,wall_mm=8)
        self.move(item,dst=wh,q=1)
        item=reserve(self.leader,item.pk,item.version,other.pk)
        plans.revise(self.leader,job.pk,job.version,dict(method='p2',length_m=Decimal('1.5'),wall_mm=Decimal(8),composition=''),'ДДК')
        item.refresh_from_db()
        self.assertEqual(item.reserved_job_id,other.pk)
        self.assertFalse(StockItem.objects.filter(reserved_job=job).exists())

    def test_unchanged_p1_parts_keep_factory_identity_when_wall_requirement_changes(self):
        from .stock_planning import reserve
        job=self.job('4','');job.wall_mm=7;job.save()
        wh=self.wh()
        ids=[]
        for n in range(2):
            item=self.item(sku='KEEP-'+str(n),kind='coupling',unit='pcs',method='p1',diameter_mm=720,length_mm=2000,wall_mm=8)
            self.move(item,dst=wh,q=1)
            reserve(self.leader,item.pk,item.version,job.pk)
            ids.append(item.pk)
        _,released=plans.revise(self.leader,job.pk,job.version,dict(method='p1',length_m=Decimal(4),wall_mm=Decimal(8),composition=''),'ДДК')
        self.assertEqual(released,[])
        self.assertEqual(set(StockItem.objects.filter(reserved_job=job).values_list('pk',flat=True)),set(ids))

    def test_p1_selects_one_wall_that_covers_the_whole_construction(self):
        job=self.make_job();wh=self.wh()
        for wall,count in [(8,1),(10,2)]:
            item=self.item(sku='WALL-'+str(wall),kind='coupling',unit='pcs',method='p1',diameter_mm=720,length_mm=1500,wall_mm=wall)
            self.move(item,dst=wh,q=count)
        plans.revise(self.leader,job.pk,job.version,dict(method='p1',length_m=Decimal(3),wall_mm=Decimal(8),composition='1.5+1.5'),'ДДК')
        held=list(StockItem.objects.filter(reserved_job=job))
        self.assertEqual(len(held),2)
        self.assertEqual({item.wall_mm for item in held},{Decimal(10)})
