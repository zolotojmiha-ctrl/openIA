import io,uuid,zipfile
from decimal import Decimal
from xml.etree import ElementTree as ET
from django.test import TestCase
from django.urls import reverse
from . import test_stock as fixture,test_stock_oversize as sized
from . import stock_planning,repair_materials,demand_mail,demand_mail_xlsx
from .models import DemandMailing,StockMovement,StockItem
from postoffice.models import Mailbox
from repairs.models import Defect,JobDefect,CouplingUse,Report


class ReservedP1DemandTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    wh=fixture.StockTests.wh;data=fixture.StockTests.data;make_job=fixture.StockTests.make_job
    balance=fixture.StockTests.balance
    setUp=sized.StockOversizeTests.setUp
    job=sized.StockOversizeTests.job;piece=sized.StockOversizeTests.piece;result=sized.StockOversizeTests.result

    def reserve(self,job,length,wall=10):
        item=self.piece(length=length,wall=wall)
        return stock_planning.reserve(self.leader,item.pk,item.version,job.pk)

    def common_job(self):
        job=self.job(length='2.5',composition='1.5+1')
        defect=job.defects.get();defect.kilometre=Decimal('123.456');defect.number='0';defect.pipe_section='TEST-PIPE-A';defect.save()
        other=Defect.objects.create(contract=self.contract,section=self.section,kilometre=Decimal('123.456'),number='TEST-DEFECT-B',pipe_section='TEST-PIPE-A')
        JobDefect.objects.create(job=job,defect=other)
        return job

    def record(self,job):return next(row for row in repair_materials.all_demand(self.leader) if row['job'].pk==job.pk)

    def mailing(self):
        box=Mailbox.objects.create(address='noreply@example.org',state='active',created_by=self.leader)
        config=DemandMailing.objects.create(name='ТЕСТ',owner=self.leader,mailbox=box,recipients='customer@example.org')
        config.project_objects.add(self.obj)
        return config

    def test_common_2500_with_reserved_1500_1000_is_covered_everywhere(self):
        job=self.common_job();a=self.reserve(job,1500);b=self.reserve(job,1000)
        before=list(StockMovement.objects.values_list('pk','quantity','kind'))
        row=self.record(job)
        self.assertEqual(sum(line['missing'] for line in row['couplings']),0)
        self.assertEqual(sum(line['reserved'] for line in row['couplings']),2)
        config=self.mailing();snapshot=demand_mail.snapshot(config,self.leader)
        self.assertEqual(snapshot['rows'],[]);self.assertEqual(snapshot['details'],[])
        from maxbot.demand_watch import snapshot as max_snapshot
        self.assertEqual(max_snapshot(self.leader)[1],[])
        with zipfile.ZipFile(io.BytesIO(demand_mail_xlsx.workbook(snapshot))) as archive:
            self.assertIn('Недостающих муфт нет.',''.join(ET.fromstring(archive.read('xl/worksheets/sheet1.xml')).itertext()))
        for route,key in [('stock_coverage','rows'),('repair_materials','grouped')]:
            response=self.client.get(reverse(route))
            self.assertEqual([p for row in response.context[key] if row.get('unit','шт.')=='шт.' for p in row['placements']],[])
        self.assertEqual(list(StockMovement.objects.values_list('pk','quantity','kind')),before)
        job.refresh_from_db();self.assertEqual(job.composition,'1.5+1')
        for item in (a,b):item.refresh_from_db();self.assertEqual(item.reserved_job_id,job.pk)

    def test_partial_reservation_quotes_only_remainder_and_matching_wall(self):
        job=self.common_job();self.reserve(job,1500)
        snapshot=demand_mail.snapshot(self.mailing(),self.leader)
        self.assertEqual([(r['length'],r['quantity'],Decimal(r['wall'])) for r in snapshot['rows']],[(1000,1,Decimal(10))])
        line=snapshot['details'][0]
        self.assertEqual(line['kilometres'],'123.456');self.assertEqual(line['defect_numbers'],'0; TEST-DEFECT-B')
        self.assertEqual(line['pipe_sections'],'TEST-PIPE-A');self.assertEqual(line['quantity'],1)
        with zipfile.ZipFile(io.BytesIO(demand_mail_xlsx.workbook(snapshot))) as archive:
            text=' '.join(''.join(ET.fromstring(archive.read(name)).itertext()) for name in archive.namelist() if name.startswith('xl/worksheets/') and name.endswith('.xml'))
        self.assertIn('123.456',text);self.assertNotIn('123.456; 123.456',text)

    def test_free_remainder_must_have_same_wall_and_is_not_double_allocated(self):
        job=self.common_job();self.reserve(job,1500)
        thin=self.piece(length=1000,wall=8)
        self.assertEqual(sum(line['missing'] for line in self.record(job)['couplings']),1)
        self.piece(length=1000,wall=10)
        other=self.job(length='1',wall=10)
        records=repair_materials.all_demand(self.leader)
        self.assertEqual(sum(line['missing'] for row in records for line in row['couplings']),1)
        thin.refresh_from_db();self.assertIsNone(thin.reserved_job_id)

    def test_installed_part_and_reserved_remainder_cover_one_common_job(self):
        job=self.common_job();long=self.reserve(job,1500);self.reserve(job,1000)
        self.result(job,[long.coupling],outcome='partial')
        row=self.record(job)
        self.assertEqual(sum(line['missing'] for line in row['couplings']),0)
        self.assertEqual(sum(line['need'] for line in row['couplings']),1)
        self.assertEqual(sum(line['reserved'] for line in row['couplings']),1)

    def test_an_installed_piece_with_a_stale_positive_balance_is_not_counted_twice(self):
        job=self.job(length='3.5',composition='1+2.5');item=self.reserve(job,1000)
        report=Report.objects.create(job=job,kind='partial',author=self.leader,performed_date=self.today)
        CouplingUse.objects.create(job=job,coupling=item.coupling,report=report)
        row=self.record(job)
        self.assertEqual([(line['length'],line['missing']) for line in row['couplings']],[(2500,1)])

    def test_cancelled_reservation_reappears_as_real_remainder(self):
        job=self.common_job();self.reserve(job,1500);short=self.reserve(job,1000)
        stock_planning.reserve(self.leader,short.pk,short.version,None)
        # A free piece on an eligible warehouse still covers the remainder.
        self.assertEqual(sum(line['missing'] for line in self.record(job)['couplings']),0)
        short.archived=True;short.save(update_fields=['archived'])
        self.assertEqual([(line['length'],line['missing']) for line in self.record(job)['couplings'] if line['missing']],[(1000,1)])

    def test_unreserved_old_composition_still_uses_standard_supply_plan(self):
        job=self.job(length='4',composition='1.5+1.5+1')
        self.assertEqual([(line['length'],line['need']) for line in self.record(job)['couplings']],[(2000,2)])

    def test_other_job_reservation_does_not_cover_this_job(self):
        first=self.common_job();self.reserve(first,1500);self.reserve(first,1000)
        other=self.job(length='2.5',composition='1.5+1')
        self.assertEqual([(line['length'],line['missing']) for line in self.record(other)['couplings']],[(2500,1)])

    def test_incompatible_reserved_dimensions_are_reported_not_treated_as_coverage(self):
        job=self.common_job();self.reserve(job,1500);job.wall_mm=12;job.save()
        row=self.record(job);self.assertTrue(row['coupling_issues'])
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):demand_mail.snapshot(self.mailing(),self.leader)

    def test_oversize_corrupt_reservation_does_not_hide_requirement(self):
        job=self.common_job();item=self.reserve(job,1500)
        StockItem.objects.filter(pk=item.pk).update(length_mm=11000)
        self.assertTrue(self.record(job)['coupling_issues'])

    def test_queued_old_algorithm_snapshot_is_not_sent_automatically(self):
        from unittest.mock import patch
        from postoffice.services import process_one
        from postoffice.models import Letter
        self.common_job();run=demand_mail.enqueue(self.leader,self.mailing().pk,uuid.uuid4())
        run.snapshot['basis']='shortage-v1';run.save(update_fields=['snapshot'])
        with patch('postoffice.transport.deliver') as deliver:
            self.assertTrue(process_one());deliver.assert_not_called()
        self.assertEqual(Letter.objects.get().state,'failed')
