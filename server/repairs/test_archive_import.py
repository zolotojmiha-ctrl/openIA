import io,json,tempfile,copy
from unittest.mock import patch
from decimal import Decimal
from datetime import date
from django.test import TestCase,override_settings
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError,PermissionDenied
from projects import test_stock as fixture
from projects.models import Contract,ContractObject,ProjectObject,StockMovement,StockEntry,StockItem
from documents.storage import private_path
from .models import Job,Defect,JobDefect,Report,ImportBatch
from . import archive_import as service,historical,completion_register


def plan_for(contract,area,*,undated=True):
    jobs=[]
    for i in range(2):
        day=None if i and undated else '2025-03-21'
        source={'historical_import':True,'period':'2024–2025','completion_date':day,
            'actual_method_unknown':False,'component_snapshots':[],
            'source_members':[{'source_refs':['ТЕСТ!D8'],'notes':['ТЕСТ источник'],'review':[]}],
            'actual_coating_length_m':None}
        defects=[{'kilometre':str(100+i),'number':str(900+i),'pipe_section':'123'}]
        if i==0:defects.append({'kilometre':'100.005','number':'999','pipe_section':'124'})
        jobs.append({'contract_id':str(contract.pk),'project_object_name':area.name,
          'job_fields':{'method':'p1','length_m':'3','wall_mm':'8','composition':'1.5+1.5',
            'notes':'ТЕСТ архивный перенос','state':'confirmed','assigned_to':None,'crew_assignment':None,'source':source},
          'defects':defects,'historical_result':{'performed_date':day}})
    return {'format':'armada-private-archive-plan-v1','target_contract':{'id':str(contract.pk),'expected_number':service.TITLE},
        'jobs':jobs,'payload_sha256':service.digest(jobs)}

def upload(plan):return SimpleUploadedFile('test-history.json',json.dumps(plan,ensure_ascii=False).encode())

class ArchiveImportTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    make_job=fixture.StockTests.make_job
    def setUp(self):
        self.files=tempfile.TemporaryDirectory();self.addCleanup(self.files.cleanup)
        self.settings=override_settings(PRIVATE_DOCUMENT_ROOT=self.files.name);self.settings.enable();self.addCleanup(self.settings.disable)
        self.current=self.make_job();self.archive=Contract.objects.create(legal_entity=self.company,number=service.TITLE,subject='ТЕСТ',work_kinds=['repairs'],status='planned')
        ContractObject.objects.create(contract=self.archive,project_object=self.obj)
        self.plan=plan_for(self.archive,self.obj)
    def prepare(self):return service.prepare(self.leader,self.archive.pk,upload(self.plan))
    def test_insert_is_scoped_shared_undated_and_repeat_is_noop(self):
        before=list(Job.objects.filter(contract=self.contract).values());batch=self.prepare()
        self.assertFalse(Job.objects.filter(contract=self.archive).exists())
        counts=service.apply(self.leader,batch.pk,batch.payload['digest']);self.assertEqual(counts,{'jobs':2,'defects':3,'undated':1})
        self.assertEqual(Job.objects.filter(contract=self.archive).count(),2);self.assertEqual(Defect.objects.filter(contract=self.archive).count(),3)
        self.assertEqual(Report.objects.filter(job__contract=self.archive).count(),1)
        self.assertEqual(list(Job.objects.filter(contract=self.contract).values()),before)
        self.assertFalse(StockMovement.objects.exists());self.assertFalse(StockEntry.objects.exists());self.assertFalse(StockItem.objects.exists())
        self.assertFalse(Defect.objects.filter(contract=self.archive,archived_at__isnull=False).exists())
        shared=Job.objects.filter(contract=self.archive).get(defects__number='900');self.assertEqual(shared.defects.count(),2)
        undated=Job.objects.filter(contract=self.archive).get(defects__number='901');self.assertFalse(undated.reports.exists())
        result=historical.result(undated);self.assertIsNone(result.performed_date);self.assertEqual(result.length_m,3)
        self.assertEqual(service.apply(self.leader,batch.pk,batch.payload['digest']),counts)
        self.assertEqual(self.prepare().pk,batch.pk)
        self.assertFalse(service.cancel(self.leader,batch.pk))
        self.archive.refresh_from_db();self.assertEqual(self.archive.status,'archived')
        with self.assertRaises(PermissionDenied):undated.save()
    def test_wrong_target_or_changed_target_rejected_before_writes(self):
        with self.assertRaises(ValidationError):service.prepare(self.leader,self.contract.pk,upload(self.plan))
        self.assertFalse(ImportBatch.objects.exists())
        batch=self.prepare();self.archive.number='ТЕСТ другой договор';self.archive.save()
        with self.assertRaises(ValidationError):service.apply(self.leader,batch.pk,batch.payload['digest'])
        self.assertFalse(Job.objects.filter(contract=self.archive).exists())
    def test_no_overwrite_when_an_existing_job_appears_after_preview(self):
        batch=self.prepare();Job.objects.create(contract=self.archive,section=self.section,created_by=self.leader,method='grind')
        with self.assertRaises(ValidationError):service.apply(self.leader,batch.pk,batch.payload['digest'])
        self.assertEqual(Job.objects.filter(contract=self.archive).count(),1)
    def test_corrupt_hash_period_date_and_duplicate_defect_are_rejected(self):
        for mutate in [lambda p:p.update(payload_sha256='bad'),lambda p:p['jobs'][0]['job_fields']['source'].update(period='2026'),
                       lambda p:p['jobs'][0]['historical_result'].update(performed_date='2026-01-01'),
                       lambda p:p['jobs'][0]['defects'].append(p['jobs'][0]['defects'][0])]:
            bad=copy.deepcopy(self.plan);mutate(bad)
            if bad.get('payload_sha256')!='bad':bad['payload_sha256']=service.digest(bad['jobs'])
            with self.assertRaises(ValidationError):service.prepare(self.leader,self.archive.pk,upload(bad))
        self.assertFalse(ImportBatch.objects.exists());self.assertFalse(Job.objects.filter(contract=self.archive).exists())
    def test_cancel_revocation_and_stale_version_block_application(self):
        batch=self.prepare();service.cancel(self.leader,batch.pk)
        with self.assertRaises(ValidationError):service.apply(self.leader,batch.pk,batch.payload['digest'])
        batch=self.prepare();self.archive.version+=1;self.archive.save()
        with self.assertRaises(ValidationError):service.apply(self.leader,batch.pk,batch.payload['digest'])
        with self.assertRaises(PermissionDenied):service.prepare(self.worker,self.archive.pk,upload(self.plan))
    def test_failure_mid_import_rolls_back_entire_batch(self):
        batch=self.prepare();original=Defect.objects.create;calls=0
        def create(**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:raise ValidationError('ТЕСТ ошибка')
            return original(**kwargs)
        with patch.object(Defect.objects,'create',side_effect=create):
            with self.assertRaises(ValidationError):service.apply(self.leader,batch.pk,batch.payload['digest'])
        self.assertFalse(Job.objects.filter(contract=self.archive).exists());self.assertFalse(Defect.objects.filter(contract=self.archive).exists())
        batch.refresh_from_db();self.assertIsNone(batch.applied_at);self.archive.refresh_from_db();self.assertEqual(self.archive.status,'planned')
    def test_multi_area_section_mismatch_rejected(self):
        self.plan['jobs'][1]['project_object_name']='ТЕСТ чужой участок';self.plan['payload_sha256']=service.digest(self.plan['jobs'])
        with self.assertRaises(ValidationError):self.prepare()
    def test_old_single_section_import_cannot_apply_archive_payload(self):
        batch=self.prepare();self.client.force_login(self.leader)
        self.assertEqual(self.client.get(reverse('repair_import_review',args=[batch.pk])).status_code,404)
    def test_browser_preview_confirmation_and_export_preserve_missing_date(self):
        self.client.force_login(self.leader)
        response=self.client.post(reverse('repair_archive_upload',args=[self.archive.pk]),{'file':upload(self.plan)})
        self.assertEqual(response.status_code,302);page=self.client.get(response.url)
        self.assertContains(page,'Не указана');self.assertFalse(Job.objects.filter(contract=self.archive).exists())
        token=page.context['form'].initial['token'];response=self.client.post(response.url,{'token':token,'confirm':'on'})
        self.assertEqual(response.status_code,302)
        page=self.client.get(reverse('repair_completed'),{'contract':str(self.archive.pk)})
        self.assertContains(page,'Не указана');self.assertEqual(page.context['summary']['defects'],3)
        groups=page.context['groups'];rows=completion_register.export_rows(groups)
        self.assertEqual(sum(row[7]=='' for row in rows),1)
        self.assertTrue(all(row[13] is None for row in rows))
        self.assertEqual(sum(row[11] or 0 for row in rows),6)
    def test_default_open_then_completed_and_archive_scoped_default(self):
        self.current.state='confirmed';self.current.save()
        self.client.force_login(self.leader)
        page=self.client.get(reverse('repairs'));self.assertEqual(page.context['state'],'open');self.assertFalse(page.context['groups'])
        page=self.client.get(reverse('repairs'),{'state':'confirmed'});self.assertEqual(len(page.context['groups']),1)
        batch=self.prepare();service.apply(self.leader,batch.pk,batch.payload['digest'])
        page=self.client.get(reverse('work_place',args=[self.archive.pk,self.obj.pk]));self.assertEqual(page.context['state'],'confirmed');self.assertEqual(len(page.context['groups']),2)
        page=self.client.get(reverse('work_place',args=[self.contract.pk,self.obj.pk]));self.assertFalse(page.context['groups'])

    def test_original_component_label_and_serial_are_evidence_not_stock(self):
        part={'method':'П1','mount_date':'2024-12-11','length_mm':2000,'wall_mm':None,'serial_as_written':'ТЕСТ-10449','source_sheet':'ТЕСТ','source_cell':'W8'}
        self.plan['jobs'][0]['job_fields']['source']['component_snapshots']=[part]
        self.plan['payload_sha256']=service.digest(self.plan['jobs'])
        batch=self.prepare();service.apply(self.leader,batch.pk,batch.payload['digest'])
        job=Job.objects.filter(contract=self.archive).get(defects__number='900')
        self.assertEqual(job.reports.get().coupling_snapshot,[part])
        self.client.force_login(self.leader)
        page=self.client.get(reverse('repair_detail',args=[job.pk]))
        self.assertContains(page,'ТЕСТ-10449');self.assertContains(page,'2024-12-11')
        from .models import Coupling,CouplingUse
        self.assertFalse(Coupling.objects.exists());self.assertFalse(CouplingUse.objects.exists())
    def test_nonfinite_metadata_is_rejected_before_storage(self):
        self.plan['jobs'][0]['job_fields']['source']['opaque']=float('nan')
        self.plan['payload_sha256']=service.digest(self.plan['jobs'])
        with self.assertRaises(ValidationError):self.prepare()
        self.assertFalse(ImportBatch.objects.exists())
