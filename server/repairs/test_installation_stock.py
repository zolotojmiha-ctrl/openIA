import uuid
from decimal import Decimal
from django.test import TestCase, override_settings
from django.core.exceptions import ValidationError
from accounts.models import AuditEvent
from projects import stock_planning
from projects.models import StockItem, StockMovement
from projects import test_stock as fixtures
from .models import Coupling, CouplingUse, Report
from .services import report_work


class InstallationStockTests(TestCase):
    setUpTestData=classmethod(fixtures.StockTests.setUpTestData.__func__)
    wh=fixtures.StockTests.wh;data=fixtures.StockTests.data;make_job=fixtures.StockTests.make_job
    balance=fixtures.StockTests.balance

    def setUp(self):
        self.job=self.make_job();self.job.method='p1';self.job.length_m=2;self.job.wall_mm=10;self.job.composition='2';self.job.save()
        self.warehouse=self.wh(project_object=self.obj)

    def receive(self,length=2000,wall=10,count=1):
        before=set(StockItem.objects.values_list('pk',flat=True))
        stock_planning.receive(self.leader,self.data(kind='coupling',unit='pcs',method='p1',
            diameter_mm=self.section.diameter_mm,length_mm=length,wall_mm=wall),self.warehouse,count,self.today,'ТЕСТ',uuid.uuid4())
        return list(StockItem.objects.exclude(pk__in=before))

    def payload(self,serial='ТЕСТ МОНТАЖ',wall=10,source=None,**changes):
        piece={'serial':serial,'length_mm':2000}
        if source:piece['source']=str(source.pk)
        return {'token':uuid.uuid4(),'expected_version':self.job.version,'performed_date':self.today,
            'outcome':'complete','method':'p1','length_m':Decimal('2'),'composition':'2','note':'ТЕСТ',
            'latitude':Decimal(55),'longitude':Decimal(73),'files':[],
            'installation':{'kind':'manual','method':'p1','wall_mm':str(wall),'pieces':[piece]},**changes}

    def send(self,**kw):return report_work(self.worker,self.job.pk,'result',self.payload(**kw))

    def test_free_unnumbered_stock_gets_actual_serial_and_consumed_before_acceptance(self):
        item=self.receive()[0]
        data=self.payload();report=report_work(self.worker,self.job.pk,'result',data)
        item.refresh_from_db();self.job.refresh_from_db()
        self.assertEqual((item.serial,item.coupling.serial),('ТЕСТ МОНТАЖ','ТЕСТ МОНТАЖ'))
        self.assertEqual((self.balance(self.warehouse,item),self.job.state),(0,'reported'))
        self.assertEqual(Coupling.objects.count(),1)
        self.assertEqual(StockMovement.objects.get(kind='consume').report_id,report.pk)
        self.assertEqual(report_work(self.worker,self.job.pk,'result',data).pk,report.pk)
        self.assertEqual(StockMovement.objects.filter(kind='consume').count(),1)

    def test_actual_wall_is_exact_and_wrong_wall_remains_free(self):
        wrong=self.receive(wall=8)[0];right=self.receive(wall=10)[0]
        self.send()
        self.assertEqual(self.balance(self.warehouse,wrong),1)
        self.assertEqual(self.balance(self.warehouse,right),0)

    def test_missing_stock_does_not_create_a_phantom_report_or_coupling(self):
        with self.assertRaises(ValidationError):self.send()
        self.assertFalse(Report.objects.exists());self.assertFalse(Coupling.objects.exists())

    def test_empty_wall_is_rejected_without_changes(self):
        item=self.receive()[0]
        with self.assertRaises(ValidationError):self.send(wall='')
        self.assertEqual(self.balance(self.warehouse,item),1)
        self.assertFalse(Report.objects.exists())

    def donor(self,item):
        job=self.make_job();job.method='p1';job.length_m=2;job.wall_mm=10;job.composition='2';job.save()
        return job,stock_planning.reserve(self.leader,item.pk,item.version,job.pk)

    def test_free_stock_precedes_other_reservation(self):
        reserved,free=self.receive(count=2);donor,reserved=self.donor(reserved)
        self.send();reserved.refresh_from_db();free.refresh_from_db()
        self.assertEqual(reserved.reserved_job_id,donor.pk)
        self.assertEqual((self.balance(self.warehouse,reserved),self.balance(self.warehouse,free)),(1,0))

    def test_reserved_source_requires_explicit_choice_then_preserves_transfer_history(self):
        item=self.receive()[0];donor,item=self.donor(item);old_version=donor.version
        with self.assertRaises(ValidationError):self.send()
        report=self.send(source=item)
        item.refresh_from_db();donor.refresh_from_db()
        self.assertEqual(item.reserved_job_id,self.job.pk)
        self.assertEqual(self.balance(self.warehouse,item),0)
        self.assertEqual(donor.version,old_version+1)
        self.assertFalse(CouplingUse.objects.filter(job=donor).exists())
        self.assertEqual(report.context['stock_bindings'][0]['previous_job'],str(donor.pk))
        self.assertTrue(AuditEvent.objects.filter(action='stock_installation_bound',details__previous_job=str(donor.pk)).exists())

    def test_two_donors_choose_specific_job_without_touching_other(self):
        a,b=self.receive(count=2);donor_a,a=self.donor(a);donor_b,b=self.donor(b)
        self.send(source=b)
        a.refresh_from_db();b.refresh_from_db()
        self.assertEqual(a.reserved_job_id,donor_a.pk)
        self.assertEqual(b.reserved_job_id,self.job.pk)

    def test_forged_or_incompatible_source_rolls_back(self):
        wrong=self.receive(wall=8)[0]
        with self.assertRaises(ValidationError):self.send(source=wrong)
        self.assertEqual(self.balance(self.warehouse,wrong),1)
        self.assertFalse(Report.objects.exists())

    def test_existing_serial_with_other_dimensions_cannot_be_relabelled(self):
        item=self.receive()[0]
        Coupling.objects.create(project_object=self.obj,method='p1',serial='ТЕСТ МОНТАЖ',length_mm=1500,wall_mm=10)
        with self.assertRaises(ValidationError):self.send()
        self.assertEqual(self.balance(self.warehouse,item),1)

    def test_all_pieces_roll_back_when_later_piece_missing(self):
        item=self.receive()[0];data=self.payload(length_m=Decimal('4'),composition='2+2')
        data['installation']['pieces'].append({'serial':'ТЕСТ ВТОРАЯ','length_mm':2000})
        with self.assertRaises(ValidationError):report_work(self.worker,self.job.pk,'result',data)
        item.refresh_from_db()
        self.assertEqual((item.serial,self.balance(self.warehouse,item)),('',1))
        self.assertFalse(Report.objects.exists());self.assertFalse(StockMovement.objects.filter(kind='consume').exists())

    def test_preexisting_unlinked_installation_record_is_reused(self):
        item=self.receive()[0]
        coupling=Coupling.objects.create(project_object=self.obj,method='p1',serial='ТЕСТ МОНТАЖ',length_mm=2000,wall_mm=10,source={'reported_without_stock':True})
        self.send();item.refresh_from_db()
        self.assertEqual(item.coupling_id,coupling.pk)
        self.assertEqual(Coupling.objects.count(),1)

    def test_multiple_free_warehouses_require_actual_source(self):
        first=self.receive()[0]
        second_wh=self.wh('ТЕСТ другой склад',project_object=self.obj)
        original=self.warehouse;self.warehouse=second_wh
        second=self.receive()[0];self.warehouse=original
        with self.assertRaises(ValidationError):self.send()
        self.send(source=second)
        self.assertEqual(self.balance(original,first),1)
        self.assertEqual(self.balance(second_wh,second),0)

    def test_known_actual_number_identifies_reserved_piece_even_when_free_exists(self):
        item,free=self.receive(count=2);donor,item=self.donor(item)
        item.serial='ТЕСТ ФАКТИЧЕСКАЯ';item.save()
        coupling=item.coupling;coupling.serial=item.serial;coupling.save()
        report=self.send(serial=item.serial)
        self.assertEqual(self.balance(self.warehouse,item),0)
        self.assertEqual(self.balance(self.warehouse,free),1)
        self.assertEqual(report.context['stock_bindings'][0]['previous_job'],str(donor.pk))

    def test_two_pieces_can_be_split_from_legacy_bulk_stock_atomically(self):
        from projects import stock
        item=stock.save_item(self.leader,None,None,self.data(kind='coupling',unit='pcs',method='p1',diameter_mm=self.section.diameter_mm,length_mm=2000,wall_mm=10))
        stock.move(self.leader,item.pk,'receipt',2,None,self.warehouse.pk,self.today,'ТЕСТ старая группа',uuid.uuid4())
        data=self.payload(length_m=Decimal('4'),composition='2+2')
        data['installation']['pieces'].append({'serial':'ТЕСТ ВТОРАЯ','length_mm':2000})
        report=report_work(self.worker,self.job.pk,'result',data)
        self.assertEqual(StockMovement.objects.filter(kind='consume',report=report).count(),2)
        self.assertEqual(CouplingUse.objects.filter(job=self.job).count(),2)
        self.assertFalse(StockItem.objects.filter(balances__quantity__gt=0).exists())

    def test_legacy_bulk_shared_between_warehouses_uses_explicit_actual_warehouse(self):
        from projects import stock
        from .installation_stock import source_key
        second_wh=self.wh('ТЕСТ другой склад',project_object=self.obj)
        item=stock.save_item(self.leader,None,None,self.data(kind='coupling',unit='pcs',method='p1',diameter_mm=self.section.diameter_mm,length_mm=2000,wall_mm=10))
        stock.move(self.leader,item.pk,'receipt',2,None,self.warehouse.pk,self.today,'ТЕСТ',uuid.uuid4())
        stock.move(self.leader,item.pk,'transfer',1,self.warehouse.pk,second_wh.pk,self.today,'ТЕСТ',uuid.uuid4())
        with self.assertRaises(ValidationError):self.send()
        data=self.payload();data['installation']['pieces'][0]['source']=source_key(item.pk,second_wh.pk)
        report_work(self.worker,self.job.pk,'result',data)
        self.assertEqual(self.balance(self.warehouse,item),1)
        self.assertEqual(self.balance(second_wh,item),0)
        movement=StockMovement.objects.get(kind='consume')
        self.assertEqual(movement.source_id,second_wh.pk)

    def test_actual_override_releases_unused_own_reservation_on_completion(self):
        reserved=self.receive(wall=12)[0];free=self.receive(wall=10)[0]
        stock_planning.reserve(self.leader,reserved.pk,reserved.version,self.job.pk)
        data=self.payload();data['installation']['override']=True
        report=report_work(self.worker,self.job.pk,'result',data)
        reserved.refresh_from_db();self.assertIsNone(reserved.reserved_job_id)
        self.assertEqual(self.balance(self.warehouse,reserved),1)
        self.assertEqual(self.balance(self.warehouse,free),0)
        self.assertIn(str(reserved.pk),report.context['released_stock_reservations'])

    def test_actual_override_keeps_unused_reservation_for_partial_work(self):
        reserved=self.receive(wall=12)[0];self.receive(wall=10)
        stock_planning.reserve(self.leader,reserved.pk,reserved.version,self.job.pk)
        data=self.payload(outcome='partial');data['installation']['override']=True
        report_work(self.worker,self.job.pk,'result',data)
        reserved.refresh_from_db();self.assertEqual(reserved.reserved_job_id,self.job.pk)
