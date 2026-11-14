import copy,json,tempfile
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError,PermissionDenied
from django.utils import timezone
from django.urls import reverse
from projects import test_stock as fixture
from projects.models import Contract,ContractObject,ContractClosureEvent,StockMovement,StockEntry
from . import completed_import as service,historical,completion_register
from .models import Job,Defect,JobDefect,Report,ImportBatch


def plan_for(contract,area):
    rows=[]
    for i in range(2):
        day='2026-03-21' if i==0 else None
        rows.append({'object':area.name,'defects':[{'kilometre':str(100+i),'number':str(900+i),'pipe_section':str(800+i)}],
            'fields':{'method':'p1','length_m':'3','wall_mm':'8','composition':'1.5+1.5','notes':'ТЕСТ выполнено по источнику'},
            'date':day,'source':{'period':'2026','completion_date':day,'source_members':[{
                'source_refs':['ТЕСТ!C8'],'evidence':{'kind':'green_kilometre','text':'ТЕСТ зелёный километр'},'notes':[],'review':[]}],
                'component_snapshots':[],'original_register_length_m':'2.5','original_register_composition':'1.5+1.5'}})
    return {'format':service.FORMAT,'target_contract':{'id':str(contract.pk),'number_token':service.NUMBER_TOKEN},
            'jobs':rows,'payload_sha256':service.digest(rows)}


def upload(plan):return SimpleUploadedFile('test-completed.json',json.dumps(plan,ensure_ascii=False).encode())


class CompletedImportTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    make_job=fixture.StockTests.make_job
    def setUp(self):
        self.files=tempfile.TemporaryDirectory();self.addCleanup(self.files.cleanup)
        cfg=override_settings(PRIVATE_DOCUMENT_ROOT=self.files.name);cfg.enable();self.addCleanup(cfg.disable)
        self.contract.number='ТЕСТ Новосибирское РНУ 2026 №ТЗС-1731-2025';self.contract.status='active';self.contract.save()
        self.current=self.make_job();self.plan=plan_for(self.contract,self.obj)
    def prepare(self):return service.prepare(self.leader,self.contract.pk,upload(self.plan))
    def test_insert_only_completed_records_and_nullable_actual_date(self):
        before=list(Job.objects.filter(pk=self.current.pk).values());batch=self.prepare()
        self.assertEqual(service.totals(batch),{'create':2,'skip':0,'hold':0,'undated':1})
        self.assertEqual(Job.objects.count(),1)
        result=service.apply(self.leader,batch.pk,batch.payload['digest'])
        self.assertEqual(result,{'create':2,'skip':0,'hold':0,'undated':1})
        self.contract.refresh_from_db();self.assertEqual(self.contract.status,'active')
        self.assertEqual(list(Job.objects.filter(pk=self.current.pk).values()),before)
        rows=Job.objects.filter(source__completed_register_import=True).order_by('defects__kilometre')
        self.assertEqual(rows.count(),2);self.assertEqual(set(rows.values_list('state',flat=True)),{'confirmed'})
        self.assertEqual(Report.objects.count(),1)
        self.assertEqual(historical.result(rows.last()).performed_date,None)
        self.assertIsNotNone(historical.result(rows.first()).performed_date)
        self.assertFalse(rows.first().source.get('historical_import'))
        self.assertFalse(ContractClosureEvent.objects.exists());self.assertFalse(StockMovement.objects.exists());self.assertFalse(StockEntry.objects.exists())
        self.assertEqual(service.apply(self.leader,batch.pk,batch.payload['digest']),result)
        self.assertEqual(self.prepare().pk,batch.pk)
        self.assertEqual(Job.objects.count(),3)
    def existing(self,row,state):
        job=Job.objects.create(contract=self.contract,section=self.section,created_by=self.leader,assigned_to=self.worker,state=state,method='p2',length_m=1,notes='ТЕСТ сохранить')
        defect=Defect.objects.create(contract=self.contract,section=self.section,**row['defects'][0]);JobDefect.objects.create(job=job,defect=defect);return job
    def test_existing_completed_skipped_working_held_without_overwrite(self):
        first=self.existing(self.plan['jobs'][0],'confirmed');second=self.existing(self.plan['jobs'][1],'progress')
        before=list(Job.objects.order_by('pk').values());batch=self.prepare()
        self.assertEqual(service.totals(batch),{'create':0,'skip':1,'hold':1,'undated':0})
        service.apply(self.leader,batch.pk,batch.payload['digest'])
        self.assertEqual(list(Job.objects.order_by('pk').values()),before)
        self.assertEqual(Report.objects.count(),0)
    def test_2026_filter_includes_imports_without_inventing_plan_or_actual_dates(self):
        from .filters import filtered_jobs
        batch=self.prepare();service.apply(self.leader,batch.pk,batch.payload['digest'])
        found=filtered_jobs(self.leader,{'contract':str(self.contract.pk),'year':'2026'}).filter(source__completed_register_import=True)
        self.assertEqual(found.count(),2)
        self.assertFalse(found.exclude(planned_date=None).exists())
        self.assertEqual(filtered_jobs(self.leader,{'year':'2025'}).filter(source__completed_register_import=True).count(),0)
    def test_changed_coordinate_or_section_is_not_duplicate_insert(self):
        job=self.existing(self.plan['jobs'][0],'confirmed');d=job.defects.get();d.kilometre+=1;d.save()
        batch=self.prepare();self.assertEqual(service.totals(batch)['hold'],1)
        service.apply(self.leader,batch.pk,batch.payload['digest']);self.assertEqual(Defect.objects.filter(number='900').count(),1)
    def test_changed_base_after_preview_requires_new_review(self):
        batch=self.prepare();self.existing(self.plan['jobs'][0],'planned')
        with self.assertRaises(ValidationError):service.apply(self.leader,batch.pk,batch.payload['digest'])
        self.assertFalse(Job.objects.filter(source__completed_register_import=True).exists())
    def test_existing_matching_job_changed_after_preview_is_detected(self):
        j=self.existing(self.plan['jobs'][0],'confirmed');batch=self.prepare();j.notes='ТЕСТ новая заметка';j.save()
        with self.assertRaises(ValidationError):service.apply(self.leader,batch.pk,batch.payload['digest'])
    def test_all_insertions_roll_back_on_midway_failure(self):
        batch=self.prepare();create=JobDefect.objects.create;calls=[]
        def fail(**kw):
            calls.append(kw)
            if len(calls)==2:raise RuntimeError('ТЕСТ сбой')
            return create(**kw)
        with patch('repairs.completed_import.JobDefect.objects.create',side_effect=fail):
            with self.assertRaises(RuntimeError):service.apply(self.leader,batch.pk,batch.payload['digest'])
        self.assertEqual(Job.objects.count(),1);self.assertEqual(Report.objects.count(),0)
        batch.refresh_from_db();self.assertIsNone(batch.applied_at)
    def test_target_identity_active_status_access_and_link_are_rechecked(self):
        with self.assertRaises(PermissionDenied):service.prepare(self.worker,self.contract.pk,upload(self.plan))
        bad=copy.deepcopy(self.plan);bad['target_contract']['id']='00000000-0000-0000-0000-000000000000'
        with self.assertRaises(ValidationError):service.prepare(self.leader,self.contract.pk,upload(bad))
        batch=self.prepare();ContractObject.objects.filter(contract=self.contract).delete()
        with self.assertRaises(ValidationError):service.apply(self.leader,batch.pk,batch.payload['digest'])
        self.assertEqual(Job.objects.count(),1)
    def test_wrong_contract_archive_and_future_year_are_rejected(self):
        self.contract.number='ТЕСТ другой договор';self.contract.save()
        with self.assertRaises(ValidationError):self.prepare()
        self.contract.number=service.NUMBER_TOKEN;self.contract.status='planned';self.contract.save()
        with self.assertRaises(ValidationError):self.prepare()
        self.contract.status='active';self.contract.save()
        self.plan['jobs'][0]['date']='2027-01-01';self.plan['jobs'][0]['source']['completion_date']='2027-01-01';self.plan['payload_sha256']=service.digest(self.plan['jobs'])
        with self.assertRaises(ValidationError):self.prepare()
    def test_bad_evidence_and_metadata_are_rejected(self):
        for change in [{'source_members':'wrong'},{'source_members':[{'evidence':{'kind':'plan'}}]},{'component_snapshots':{}},{'component_snapshots':[None]}]:
            with self.subTest(change=change):
                p=copy.deepcopy(self.plan);p['jobs'][0]['source'].update(change);p['payload_sha256']=service.digest(p['jobs'])
                with self.assertRaises(ValidationError):service.prepare(self.leader,self.contract.pk,upload(p))
        self.assertFalse(ImportBatch.objects.exists())
    def test_cancel_expiry_and_session_recheck(self):
        batch=self.prepare();self.assertTrue(service.cancel(self.leader,batch.pk))
        with self.assertRaises(ValidationError):service.apply(self.leader,batch.pk,batch.payload['digest'])
        batch=self.prepare();ImportBatch.objects.filter(pk=batch.pk).update(created_at=timezone.now()-timedelta(days=2))
        with self.assertRaises(ValidationError):service.apply(self.leader,batch.pk,batch.payload['digest'])
        batch=self.prepare();type(self.leader).objects.filter(pk=self.leader.pk).update(session_version=self.leader.session_version+1)
        with self.assertRaises(PermissionDenied):service.apply(self.leader,batch.pk,batch.payload['digest'])
    def test_lk_upload_preview_and_checked_confirmation(self):
        self.client.force_login(self.leader)
        response=self.client.post(reverse('repair_completed_upload',args=[self.contract.pk]),{'file':upload(self.plan)})
        batch=ImportBatch.objects.get();self.assertRedirects(response,reverse('repair_completed_review',args=[batch.pk]))
        page=self.client.get(response.url);self.assertContains(page,'Добавить как устранённый')
        token=page.context['form'].initial['token']
        no=self.client.post(response.url,{'token':token});self.assertEqual(Job.objects.count(),1)
        applied=self.client.post(response.url,{'token':token,'confirm':'on'})
        self.assertEqual(applied.status_code,302);self.assertEqual(Job.objects.count(),3)
        self.assertContains(self.client.get(reverse('repair_completed_review',args=[batch.pk])),'Перенос уже выполнен')
