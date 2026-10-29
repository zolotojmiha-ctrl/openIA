import copy,json,os,subprocess,uuid
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import SimpleTestCase,TestCase
from django.urls import reverse
from projects import test_stock as base
from projects.models import LegalEntity,COMPANY_TAX_ID,StockItem
from projects import stock_planning
from .construction import planned_parts
from .forms import JobForm
from .models import ImportBatch,Job
from .tests import source_rows,workbook
from .importer import parse_work

class ConstructionValuesTests(SimpleTestCase):
    def test_total_can_be_derived_but_conflicting_explicit_total_cannot_be_overwritten(self):
        self.assertEqual(planned_parts(None,'1+1,5'),(Decimal('2.5'),[Decimal(1),Decimal('1.5')]))
        self.assertEqual(planned_parts('1',''),(Decimal(1),[Decimal(1)]))
        with self.assertRaisesMessage(ValidationError,'длина 1 м, а сумма состава 1,5 м'):planned_parts('1','1,5')
    def test_invalid_parts_cannot_create_a_demand(self):
        for length,composition in [(None,''),(1,'0+1'),(1,'1+abc'),(None,'0,0001'),('NaN',''),('Infinity',''),(None,'999999+999999')]:
            with self.subTest(length=length,composition=composition),self.assertRaises(ValidationError):planned_parts(length,composition)

class PlannedInputTests(TestCase):
    setUpTestData=classmethod(base.StockTests.setUpTestData.__func__)
    wh=base.StockTests.wh;data=base.StockTests.data;make_job=base.StockTests.make_job
    def setUp(self):self.client.force_login(self.leader)
    def payload(self,**changes):
        return {'contract':str(self.contract.pk),'section':str(self.section.pk),'expected_version':'0','kilometre':'12.345','defect_numbers':'12345','pipe_section':'','method':'p2','length_m':'1','wall_mm':'8','composition':'','planned_date':'','assigned_to':'','materials':'unknown','notes':'',**changes}
    def batch(self,**changes):
        item={'rows':[2],'defects':[{'km':'12.345','number':'12345','pipe_section':''}],'method':'p2','length_m':'1','wall_mm':'8','composition':'1,5','planned_date':'','note':'','raw':[{'row':2,'cells':{'G':'1','H':'1,5'}}],'candidates':[],'assigned_to':None,**changes}
        return ImportBatch.objects.create(contract=self.contract,section=self.section,author=self.leader,filename='synthetic.xlsx',sha256=uuid.uuid4().hex*2,sheet='Тест',payload={'jobs':[item],'couplings':[],'warnings':[]})
    def test_manual_conflict_is_rejected_before_job_is_saved(self):
        response=self.client.post(reverse('repair_new'),self.payload(composition='1,5'))
        self.assertEqual(response.status_code,200)
        self.assertIn('composition',response.context['form'].errors)
        self.assertFalse(Job.objects.exists())
    def test_manual_total_is_calculated_if_only_composition_was_entered(self):
        response=self.client.post(reverse('repair_new'),self.payload(length_m='',composition='1+1,5'))
        self.assertEqual(response.status_code,302,response.context and response.context['form'].errors)
        self.assertEqual(Job.objects.get().length_m,Decimal('2.5'))
    def test_old_invalid_card_can_be_corrected_without_changing_identity(self):
        job=self.make_job();job.composition='1,5';job.save();defect=job.defects.get()
        response=self.client.post(reverse('repair_edit',args=[job.pk]),self.payload(expected_version=job.version,kilometre=str(defect.kilometre),defect_numbers=defect.number,composition=''))
        self.assertEqual(response.status_code,302,response.context and response.context['form'].errors)
        job.refresh_from_db();self.assertEqual(job.length_m,1);self.assertEqual(job.composition,'')
        self.assertEqual(job.defects.get().pk,defect.pk)
    def test_old_pending_preview_cannot_import_conflict_even_with_an_old_post(self):
        batch=self.batch();response=self.client.post(reverse('repair_import_review',args=[batch.pk]),{'row_0':'new'})
        self.assertEqual(response.status_code,200)
        self.assertIn('composition_0',response.context['form'].errors)
        self.assertFalse(Job.objects.exists());batch.refresh_from_db();self.assertIsNone(batch.applied_at)
    def test_preview_can_correct_plan_and_keeps_original_source(self):
        batch=self.batch();original=copy.deepcopy(batch.payload)
        response=self.client.post(reverse('repair_import_review',args=[batch.pk]),{'row_0':'new','length_0':'1','composition_0':''})
        self.assertEqual(response.status_code,302,response.context and response.context['form'].errors)
        job=Job.objects.get();self.assertEqual(job.length_m,1);self.assertEqual(job.composition,'')
        self.assertEqual(job.source['reviewed_plan']['original_composition'],'1,5')
        batch.refresh_from_db();self.assertEqual(batch.payload,original)
    def test_invalid_new_row_blocks_whole_import_until_it_is_fixed_or_skipped(self):
        batch=self.batch();good=copy.deepcopy(batch.payload['jobs'][0]);good.update(composition='',defects=[{'km':'12.346','number':'12346','pipe_section':''}])
        batch.payload['jobs'].append(good);batch.save()
        url=reverse('repair_import_review',args=[batch.pk])
        self.assertEqual(self.client.post(url,{'row_0':'new','row_1':'new'}).status_code,200)
        self.assertFalse(Job.objects.exists());batch.refresh_from_db();self.assertIsNone(batch.applied_at)
        self.assertEqual(self.client.post(url,{'row_0':'skip','row_1':'new'}).status_code,302)
        self.assertEqual(Job.objects.get().defects.get().number,'12346')

    def test_preview_derives_total_and_skip_does_not_require_correction(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .excel import workbook as standard_book
        parsed=parse_work(SimpleUploadedFile('synthetic.xlsx',standard_book([['12.345','12345','П2','','1+1,5']],headers=['Км','№ дефекта','Метод ремонта','Длина, м','Состав конструкции, м'])))
        self.assertEqual(parsed['jobs'][0]['length_m'],'')
        self.assertEqual(parsed['jobs'][0]['composition'],'1+1,5')
        batch=self.batch();response=self.client.post(reverse('repair_import_review',args=[batch.pk]),{'row_0':'new','length_0':'','composition_0':'1+1,5'})
        self.assertEqual(response.status_code,302,response.context and response.context['form'].errors)
        self.assertEqual(Job.objects.get().length_m,Decimal('2.5'))
        batch=self.batch();response=self.client.post(reverse('repair_import_review',args=[batch.pk]),{'row_0':'skip','length_0':'invalid','composition_0':'invalid'})
        self.assertEqual(response.status_code,302,response.context and response.context['form'].errors)
        self.assertEqual(Job.objects.count(),1)
    def test_valid_minimum_plan_stays_unchanged_when_a_longer_piece_is_reserved(self):
        job=self.make_job();warehouse=self.wh()
        stock_planning.receive(self.leader,self.data(kind='coupling',unit='pcs',method='p2',length_mm=1500,wall_mm=10),warehouse,1,self.today,'Тест',uuid.uuid4())
        item=StockItem.objects.get();stock_planning.reserve(self.leader,item.pk,item.version,job.pk)
        job.refresh_from_db();self.assertEqual(job.length_m,1);self.assertEqual(job.composition,'')
    def test_legacy_workbook_preview_warns_instead_of_hiding_conflict(self):
        rows=source_rows();rows[7]['G']=1
        result=parse_work(workbook(rows,merges=['F7:F8','G7:G8','H7:H8','C7:C8']))
        self.assertTrue(any('длина 1 м, а сумма состава 3 м' in warning for warning in result['warnings']))

class PlannedConstructionBrowser(StaticLiveServerTestCase):
    def test_manual_and_import_corrections_in_browser(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'Тест'})
        base.StockTests.setUpTestData.__func__(type(self))
        self.leader.set_password('123456');self.leader.save()
        fixture=PlannedInputTests();fixture.contract=self.contract;fixture.section=self.section;fixture.leader=self.leader
        batch=fixture.batch()
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/planned-construction-browser.cjs')],env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_PLAN_FIXTURE':json.dumps({'contract':str(self.contract.pk),'section':str(self.section.pk),'batch':str(batch.pk)})},capture_output=True,text=True,timeout=100)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(Job.objects.count(),2)
        self.assertEqual(set(Job.objects.values_list('length_m',flat=True)),{Decimal(1),Decimal('2.5')})
