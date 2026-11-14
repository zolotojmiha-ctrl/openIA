import copy,tempfile
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from django.test import TestCase,SimpleTestCase,override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import PermissionDenied,ValidationError
from django.urls import reverse
from projects import test_stock as fixture
from projects.models import Contract,ContractObject,ProjectObject,ObjectAccess,ContractAccess
from accounts.models import User
from .models import Section,PipeLayout,Job,Defect,JobDefect
from .excel import workbook
from .layout_reader import parse
from . import layouts

ROWS=[['100','0','3000','110','A',2,'100000'],['110','3000','3000','120','A',3,'103000'],['120','6000','20','','A',4,'106000']]

def xlsx(rows=None):
    headers=['']*25;headers[4]='Номер секции';headers[5]='Дистанция начала секции';headers[6]='Длина секции,м';headers[17]='Следующая секция';headers[24]='Код прогона'
    data=[]
    for r in rows or ROWS:
        cells=['']*25
        for col,value in [(4,r[0]),(5,r[1]),(6,r[2]),(17,r[3]),(24,r[4]),(16,r[6])]:cells[col]=value
        data.append(cells)
    return SimpleUploadedFile('test-layout.xlsx',workbook(data,headers=headers,sheet_name='Секции'))

def defect(number,km):return SimpleNamespace(pipe_section=str(number),kilometre=Decimal(str(km)))

class GeometryTests(SimpleTestCase):
    def geo(self,rows=None):return layouts.Geometry(SimpleNamespace(data={'rows':copy.deepcopy(rows or ROWS)}))
    def test_sum_includes_first_length_excludes_last_and_is_reversible(self):
        geo=self.geo();a=defect(100,100);b=defect(120,106)
        self.assertEqual(geo.distance(a,b)['metres'],6000);self.assertFalse(geo.distance(a,b)['trailer'])
        self.assertEqual(geo.distance(b,a)['metres'],6000);self.assertEqual(geo.distance(a,a)['metres'],0)
        rows=copy.deepcopy(ROWS);rows[1][2]='3000.001';rows[2][1]='6000.001'
        self.assertTrue(self.geo(rows).distance(a,b)['trailer'])
    def test_chain_gap_run_change_duplicate_and_missing_fields_refuse_approximation(self):
        mutations=[lambda r:r[0].__setitem__(3,'999'),lambda r:r[1].__setitem__(4,'B'),lambda r:r[1].__setitem__(1,'3001'),lambda r:r[1].__setitem__(2,None),lambda r:r[1].__setitem__(0,'100')]
        for mutate in mutations:
            rows=copy.deepcopy(ROWS);mutate(rows)
            with self.subTest(rows=rows):self.assertIn('reason',self.geo(rows).distance(defect(100,100),defect(120,106)))
        self.assertIn('reason',self.geo().distance(defect(555,100),defect(120,106)))
    def test_old_section_number_never_substituted_by_kilometre(self):
        self.assertIn('не соответствует',self.geo().distance(defect(100,200),defect(120,106))['reason'])
    def test_reader_accepts_layout_but_not_a_defect_worksheet(self):
        data=parse(xlsx());self.assertEqual(len(data['rows']),3);self.assertEqual(data['rows'][1][0],'110')
        with self.assertRaises(ValidationError):parse(SimpleUploadedFile('defects.xlsx',workbook([['A',1,'2','3','Шлифовка']])))

class LayoutServiceTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    def setUp(self):
        root=tempfile.TemporaryDirectory();self.addCleanup(root.cleanup)
        config=override_settings(PRIVATE_DOCUMENT_ROOT=root.name);config.enable();self.addCleanup(config.disable)
    def job(self,km,num,assigned=True,contract=None):
        contract=contract or self.contract
        j=Job.objects.create(contract=contract,section=self.section,created_by=self.leader,assigned_to=self.worker if assigned else None,method='grind')
        d=Defect.objects.create(contract=contract,section=self.section,kilometre=km,pipe_section=str(num),number=str(Job.objects.count()))
        JobDefect.objects.create(job=j,defect=d);return j,d
    def selected(self):
        row=layouts.prepare(self.leader,self.section.pk,xlsx());layouts.apply(self.leader,row.pk);return row
    def test_preview_cancel_apply_repeat_versions_and_no_repair_changes(self):
        job,d=self.job(100,100);before=list(Job.objects.values());row=layouts.prepare(self.leader,self.section.pk,xlsx())
        self.assertFalse(PipeLayout.objects.filter(state='active').exists());layouts.cancel(self.leader,row.pk)
        with self.assertRaises(ValidationError):layouts.apply(self.leader,row.pk)
        row=self.selected();self.assertEqual(layouts.apply(self.leader,row.pk).pk,row.pk)
        self.assertEqual(layouts.prepare(self.leader,self.section.pk,xlsx()).pk,row.pk)
        rows=copy.deepcopy(ROWS);rows[2][2]='21';new=layouts.prepare(self.leader,self.section.pk,xlsx(rows));layouts.apply(self.leader,new.pk)
        row.refresh_from_db();self.assertEqual(row.state,'superseded');self.assertEqual(PipeLayout.objects.filter(state='active').count(),1)
        self.assertEqual(list(Job.objects.values()),before);d.refresh_from_db();self.assertEqual(d.pipe_section,'100')
    def test_stale_preview_foreign_owner_and_revoked_session_are_rejected(self):
        row=layouts.prepare(self.leader,self.section.pk,xlsx());other=User.objects.create_user('+70000880099',full_name='ТЕСТ второй',leader_slot=2,must_change_password=False)
        with self.assertRaises(PermissionDenied):layouts.apply(other,row.pk)
        with self.assertRaises(PermissionDenied):layouts.prepare(self.worker,self.section.pk,xlsx())
        rows=copy.deepcopy(ROWS);rows[2][2]='22';new=layouts.prepare(self.leader,self.section.pk,xlsx(rows));layouts.apply(self.leader,new.pk)
        with self.assertRaises(ValidationError):layouts.apply(self.leader,row.pk)
        rows[2][2]='23';stale=layouts.prepare(self.leader,self.section.pk,xlsx(rows))
        User.objects.filter(pk=self.leader.pk).update(session_version=self.leader.session_version+1)
        with self.assertRaises(PermissionDenied):layouts.apply(self.leader,stale.pk)
    def test_neighbours_respect_contract_and_reader_acl_but_not_search_or_page(self):
        self.selected();a,da=self.job(100,100);b,db=self.job(106,120);hidden,dh=self.job(103,110,assigned=False)
        other=Contract.objects.create(legal_entity=self.company,number='ТЕСТ другой',subject='ТЕСТ',work_kinds=['repairs']);ContractObject.objects.create(contract=other,project_object=self.obj)
        self.job(101,110,contract=other)
        pairs=layouts.neighbours(self.worker,[db])[db.pk]
        self.assertEqual(len(pairs),1);self.assertEqual(pairs[0]['left'].pk,da.pk);self.assertEqual(pairs[0]['metres'],6000)
        self.assertEqual(layouts.neighbours(self.leader,[db])[db.pk][0]['left'].pk,dh.pk)
    def test_lk_upload_confirmation_and_inline_distances(self):
        self.job(100,100);job,d=self.job(106,120);self.client.force_login(self.leader)
        response=self.client.post(reverse('repair_layout_upload',args=[self.section.pk]),{'file':xlsx()});self.assertEqual(response.status_code,302)
        page=self.client.get(response.url);self.assertContains(page,'Проверка раскладки');token=page.context['form'].initial['token']
        self.assertEqual(self.client.post(response.url,{'token':token,'confirm':'on'}).status_code,302)
        page=self.client.get(reverse('repair_detail',args=[job.pk]));self.assertContains(page,'6000,000 м')
        page=self.client.get(reverse('repairs'),{'q':d.number});self.assertContains(page,'6000,000 м')
    def test_apply_failure_keeps_previous_active_version(self):
        old=self.selected();rows=copy.deepcopy(ROWS);rows[2][2]='21';new=layouts.prepare(self.leader,self.section.pk,xlsx(rows))
        with patch('repairs.layouts.audit',side_effect=RuntimeError('ТЕСТ сбой')):
            with self.assertRaises(RuntimeError):layouts.apply(self.leader,new.pk)
        old.refresh_from_db();new.refresh_from_db();self.assertEqual(old.state,'active');self.assertEqual(new.state,'preview')
