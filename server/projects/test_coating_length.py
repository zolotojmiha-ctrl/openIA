import uuid
from decimal import Decimal
from django.test import TestCase
from django.core.exceptions import ValidationError
from django.urls import reverse
from repairs.forms import ReportForm
from repairs.services import report_work
from repairs.models import Report, METHODS
from . import test_p1_coating_and_undo as fixtures,test_release624 as release
from .models import RepairConsumption,StockMovement
from .p1_coating import coating as p1_coating
from .repair_materials import coating as p2_coating


class CoatingLengthTests(TestCase):
    setUpTestData=classmethod(fixtures.P1CoatingAndUndoTests.setUpTestData.__func__)
    setUp=fixtures.P1CoatingAndUndoTests.setUp
    wh=fixtures.P1CoatingAndUndoTests.wh;data=fixtures.P1CoatingAndUndoTests.data
    item=fixtures.P1CoatingAndUndoTests.item;move=fixtures.P1CoatingAndUndoTests.move
    make_job=fixtures.P1CoatingAndUndoTests.make_job
    job=fixtures.P1CoatingAndUndoTests.job;supplies=fixtures.P1CoatingAndUndoTests.supplies
    pieces=fixtures.P1CoatingAndUndoTests.pieces;balance=fixtures.P1CoatingAndUndoTests.balance
    profile=release.Release624Tests.profile

    def payload(self,job,**changes):
        return {'token':uuid.uuid4(),'expected_version':job.version,'performed_date':self.today,
                'coordinates':'55.123456, 73.123456','length_m':'1','manual_serials':'TEST-COATING',
                'actual_coating_length_m':'2,4',**changes}

    def test_all_completed_methods_require_fact_but_problem_does_not(self):
        for method,_ in METHODS:
            job=self.job('1','');job.method=method;job.save()
            form=ReportForm(self.payload(job,actual_coating_length_m=''),job=job,action='repaired',user=self.worker)
            self.assertFalse(form.is_valid());self.assertIn('actual_coating_length_m',form.errors)
            form=ReportForm(self.payload(job),job=job,action='repaired',user=self.worker)
            self.assertTrue(form.is_valid(),form.errors)
            self.assertEqual(form.cleaned_data['actual_coating_length_m'],Decimal('2.4'))
            self.assertNotIn('actual_coating_length_m',ReportForm(job=job,action='unrepaired',user=self.worker).fields)

    def test_partial_fact_is_optional_complete_fact_is_required(self):
        job=self.job('1','')
        base={'token':uuid.uuid4(),'expected_version':job.version,'performed_date':self.today,'method':'p1','length_m':'1'}
        partial=ReportForm(dict(base,outcome='partial'),job=job,action='result',user=self.worker)
        self.assertNotIn('actual_coating_length_m',partial.errors)
        self.assertTrue(partial.fields['actual_coating_length_m'].disabled)
        self.assertIsNone(partial.cleaned_data.get('actual_coating_length_m'))
        complete=ReportForm(dict(base,outcome='complete'),job=job,action='result',user=self.worker)
        self.assertIn('actual_coating_length_m',complete.errors)

    def test_p1_new_measurement_does_not_double_count_legacy_cleaning(self):
        job=self.job('1','');base=p1_coating(job)
        job.actual_coating_length_m='2.4';job.actual_cleaning_length_m='3.4'
        lines=p1_coating(job)
        self.assertEqual([line['quantity'] for line in lines],['1.575','38.034','5.932'])
        self.assertEqual(lines[0]['actual_coating_length_m'],'2.4')
        job.actual_coating_length_m='1.2'
        self.assertEqual([line['quantity'] for line in p1_coating(job)],[line['quantity'] for line in base])

    def test_p2_adds_only_extra_pipe_area_to_approved_profile(self):
        profile,_,_=self.profile(1000);job=self.make_job()
        base=p2_coating(job);norm=Decimal(profile.full_length_mm+profile.extra_length_mm)/1000
        job.actual_coating_length_m=norm+1
        lines=p2_coating(job)
        self.assertEqual(Decimal(lines[0]['extra_area_m2']),Decimal('2.26080'))
        self.assertEqual(Decimal(lines[0]['quantity'])-Decimal(base[0]['quantity']),Decimal('11.734'))
        job.actual_coating_length_m=norm-Decimal('.1')
        self.assertEqual([l['quantity'] for l in p2_coating(job)],[l['quantity'] for l in base])

    def test_saved_fact_retry_and_correction_preserve_posted_stock(self):
        job=self.job('1','');wh,items=self.supplies();pieces=self.pieces(wh,(1000,))
        data={'token':uuid.uuid4(),'expected_version':job.version,'performed_date':self.today,'outcome':'complete',
              'method':'p1','length_m':Decimal(1),'composition':'','actual_coating_length_m':Decimal('2.4'),'couplings':pieces,'files':[]}
        report=report_work(self.worker,job.pk,'result',data)
        self.assertEqual(report.context['actual_coating_length_m'],'2.4')
        self.assertEqual(RepairConsumption.objects.get(job=job).state,'posted')
        balance=self.balance(wh,items['wrapper'])
        self.assertEqual(report_work(self.worker,job.pk,'result',dict(data,actual_coating_length_m=Decimal('2.400'))).pk,report.pk)
        job.refresh_from_db();data.update(token=uuid.uuid4(),expected_version=job.version,actual_coating_length_m=Decimal('3.4'))
        report_work(self.leader,job.pk,'correction',data)
        self.assertEqual(RepairConsumption.objects.get(job=job).state,'review')
        self.assertEqual(self.balance(wh,items['wrapper']),balance)
        self.client.force_login(self.leader)
        self.assertContains(self.client.get(reverse('repair_detail',args=[job.pk])),'Фактическая длина изоляционного покрытия')

    def test_bad_measurements_are_rejected_without_writing(self):
        job=self.job('1','');before=StockMovement.objects.count()
        for value in ('0','-1','NaN','Infinity','1000000'):
            with self.assertRaises(ValidationError):
                report_work(self.worker,job.pk,'result',{'token':uuid.uuid4(),'expected_version':job.version,'actual_coating_length_m':value})
        self.assertFalse(Report.objects.exists());self.assertEqual(StockMovement.objects.count(),before)

    def test_pending_retry_restores_new_fact_from_saved_report(self):
        from . import repair_materials
        job=self.job('1','');wh,items=self.supplies(skip=('wrapper',));pieces=self.pieces(wh,(1000,))
        report=report_work(self.worker,job.pk,'result',{'token':uuid.uuid4(),'expected_version':job.version,
            'performed_date':self.today,'outcome':'complete','method':'p1','length_m':Decimal(1),
            'actual_coating_length_m':Decimal('3.4'),'couplings':pieces,'files':[]})
        self.assertEqual(RepairConsumption.objects.get(job=job).state,'pending')
        wrapper=self.item(sku='NEW-WRAPPER',name='TEST wrapper',kind='wrapper',unit='kg')
        self.move(wrapper,dst=wh,q=100)
        job.refresh_from_db();repair_materials.retry(self.leader,job)
        row=RepairConsumption.objects.get(job=job)
        self.assertEqual(row.state,'posted');self.assertEqual(row.report_id,report.pk)
        line=next(l for l in row.lines if l['kind']=='wrapper')
        self.assertEqual(line['actual_coating_length_m'],'3.4')
        self.assertEqual(Decimal(line['quantity']),Decimal('7.763'))

    def test_partial_result_cannot_store_a_completion_measurement(self):
        job=self.job('1','')
        with self.assertRaises(ValidationError):
            report_work(self.worker,job.pk,'result',{'token':uuid.uuid4(),'expected_version':job.version,
                'outcome':'partial','actual_coating_length_m':'2.4'})
        self.assertFalse(Report.objects.exists())

    def test_legacy_retry_without_fact_returns_original_report(self):
        job=self.job('1','');payload=self.payload(job)
        form=ReportForm(payload,job=job,action='repaired',user=self.worker)
        self.assertTrue(form.is_valid(),form.errors)
        old_data=dict(form.cleaned_data);old_data.pop('actual_coating_length_m')
        original=report_work(self.worker,job.pk,'result',old_data)
        payload['actual_coating_length_m']=''
        self.client.force_login(self.worker)
        response=self.client.post(reverse('repair_report',args=[job.pk,'repaired']),payload)
        self.assertEqual(response.status_code,302,response.context and response.context['form'].errors)
        self.assertEqual(Report.objects.get().pk,original.pk)

    def test_all_other_methods_store_fact_and_prepare_approved_consumption(self):
        for method,_ in METHODS:
            if method in ('p1','p2'):continue
            job=self.job('1','');job.method=method;job.save()
            form=ReportForm(self.payload(job),job=job,action='repaired',user=self.worker)
            self.assertTrue(form.is_valid(),(method,form.errors))
            report=report_work(self.worker,job.pk,'result',form.cleaned_data)
            self.assertEqual(Decimal(report.context['actual_coating_length_m']),Decimal('2.4'))
            row=RepairConsumption.objects.get(job=job)
            self.assertEqual(row.state,'pending')
            self.assertTrue(row.lines)
            self.assertEqual(next(line['quantity'] for line in row.lines if line['kind']=='primer'),'1.140')

    def test_grinding_posts_real_materials_once_and_changed_fact_requires_review(self):
        job=self.job('1','');job.method='grind';job.save()
        warehouse,items=self.supplies()
        data={'token':uuid.uuid4(),'expected_version':job.version,'performed_date':self.today,'outcome':'complete',
            'method':'grind','actual_coating_length_m':Decimal('2.4'),'couplings':[],'files':[]}
        report=report_work(self.worker,job.pk,'result',data)
        row=RepairConsumption.objects.get(job=job);self.assertEqual(row.state,'posted')
        self.assertEqual(self.balance(warehouse,items['primer']),Decimal('98.860'))
        count=StockMovement.objects.count()
        self.assertEqual(report_work(self.worker,job.pk,'result',data).pk,report.pk)
        self.assertEqual(StockMovement.objects.count(),count)
        job.refresh_from_db()
        report_work(self.leader,job.pk,'correction',dict(data,token=uuid.uuid4(),expected_version=job.version,actual_coating_length_m=Decimal('3.4')))
        row.refresh_from_db();self.assertEqual(row.state,'review');self.assertEqual(StockMovement.objects.count(),count)


    def test_legacy_grinding_correction_requires_review_instead_of_retroactive_charge(self):
        job=self.job('1','');job.method='grind';job.save();warehouse,items=self.supplies()
        data={'token':uuid.uuid4(),'expected_version':job.version,'performed_date':self.today,'outcome':'complete','method':'grind','couplings':[],'files':[]}
        report_work(self.worker,job.pk,'result',data)
        self.assertFalse(RepairConsumption.objects.filter(job=job).exists())
        job.refresh_from_db();count=StockMovement.objects.count()
        report_work(self.leader,job.pk,'correction',dict(data,token=uuid.uuid4(),expected_version=job.version,actual_coating_length_m=Decimal('2.4')))
        row=RepairConsumption.objects.get(job=job);self.assertEqual((row.state,row.review_reason),('review','legacy'))
        self.assertEqual(StockMovement.objects.count(),count)
