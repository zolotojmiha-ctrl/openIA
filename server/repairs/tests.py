import io
import secrets
import tempfile
import uuid
import zipfile
from datetime import timedelta
from decimal import Decimal
from xml.sax.saxutils import escape
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase,override_settings
from django.urls import reverse
from django.utils import timezone
from accounts.models import User
from projects.models import LegalEntity,ProjectObject,ObjectAccess,COMPANY_TAX_ID
from .models import Section,Defect,Job,JobDefect,Coupling,CouplingUse,Report,Attachment,ImportBatch
from .importer import parse_work
from .access import selectable_couplings

def workbook(rows,merges=(),name='Последний лист',external=False):
    """Synthetic workbook: no customer records or external package required."""
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as z:
        z.writestr('xl/workbook.xml',f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="{escape(name)}" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        items=[]
        for number,cells in rows.items():
            values=[]
            for col,value in cells.items():
                if isinstance(value,tuple):values.append(f'<c r="{col}{number}"><f>{escape(value[0])}</f><v>{escape(str(value[1]))}</v></c>')
                elif isinstance(value,(int,float,Decimal)):values.append(f'<c r="{col}{number}"><v>{value}</v></c>')
                else:values.append(f'<c r="{col}{number}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
            items.append(f'<row r="{number}">'+''.join(values)+'</row>')
        z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join(items)+'</sheetData><mergeCells>'+''.join(f'<mergeCell ref="{m}"/>' for m in merges)+'</mergeCells></worksheet>')
    return SimpleUploadedFile('Учебная ведомость.xlsx',data.getvalue())

def source_rows():
    return {4:{'C':'КМ','D':'№ дефекта','E':'№ секции','F':'МУФТА','G':'Длина (м)','H':'Состав муфты','S':'Дата устранения (план)','Y':'Примечание','Z':'км монтажа','AA':'Муфта','AB':'Длина','AC':'стенка','AD':'зав №','AE':'склад получения','AF':'Дата монтажа, ФАКТ'},
        7:{'C':Decimal('101.123456'),'D':'ДЕМО-Д1','E':'СЕК-01','F':'П1','G':3,'H':'1,5+1,5','S':46000,'Z':999,'AA':'П1','AB':1500,'AC':10,'AD':'0007','AE':'Учебный склад','AF':46001},
        8:{'D':'ДЕМО-Д2','Z':('C9','202.234567'),'AA':'П2','AB':1000,'AC':8,'AD':'УЧ-08','AE':'Другой учебный склад'},
        9:{'C':Decimal('202.234567'),'D':'ДЕМО-Д3','E':'СЕК-02','F':'шлифовка','Y':'Нет доступа'},
        12:{'AA':'На остатках на базе:'},
        15:{'AA':'П1','AB':1000,'AD':99}}

class RepairsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        password=secrets.token_urlsafe(24)
        cls.leader=User.objects.create_user('+70000000001',password,full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        cls.second=User.objects.create_user('+70000000002',password,full_name='ТЕСТ Второй руководитель',leader_slot=2,must_change_password=False)
        cls.worker=User.objects.create_user('+70000000003',password,full_name='ТЕСТ Мастер',role='foreman',must_change_password=False)
        cls.other=User.objects.create_user('+70000000004',password,full_name='ТЕСТ Другой мастер',role='foreman',must_change_password=False)
        company=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
        cls.obj=ProjectObject.objects.create(legal_entity=company,name='Учебный объект')
        cls.obj2=ProjectObject.objects.create(legal_entity=company,name='Чужой объект')
        cls.section=Section.objects.create(project_object=cls.obj,rnu='Учебное РНУ',pipeline='Учебный трубопровод',name='Учебный участок')
        cls.section2=Section.objects.create(project_object=cls.obj2,rnu='ЗАКРЫТОЕ РНУ',pipeline='ЗАКРЫТАЯ МАГИСТРАЛЬ',name='ЗАКРЫТЫЙ УЧАСТОК')
        ObjectAccess.objects.create(user=cls.worker,project_object=cls.obj,granted_by=cls.leader)
        cls.job=Job.objects.create(section=cls.section,assigned_to=cls.worker,method='p1',length_m=3,composition='1,5+1,5',created_by=cls.leader)
        cls.d1=Defect.objects.create(section=cls.section,kilometre=Decimal('101.123456'),number='УЧ-01',pipe_section='СЕК-01')
        cls.d2=Defect.objects.create(section=cls.section,kilometre=Decimal('101.123456'),number='УЧ-02',pipe_section='СЕК-01')
        JobDefect.objects.create(job=cls.job,defect=cls.d1);JobDefect.objects.create(job=cls.job,defect=cls.d2)
        cls.hidden=Job.objects.create(section=cls.section2,assigned_to=cls.other,method='grind',created_by=cls.leader)
        d=Defect.objects.create(section=cls.section2,kilometre=999,number='ЗАКРЫТЫЙ-ДЕФЕКТ');JobDefect.objects.create(job=cls.hidden,defect=d)
        cls.c1=Coupling.objects.create(project_object=cls.obj,method='p1',length_mm=1500,serial='УЧ-001')
        cls.c2=Coupling.objects.create(project_object=cls.obj,method='p1',length_mm=1500,serial='УЧ-002')
        cls.foreign=Coupling.objects.create(project_object=cls.obj2,method='p1',length_mm=1500,serial='ЧУЖАЯ-МУФТА')
    def setUp(self):
        self.root=tempfile.TemporaryDirectory();self.settings=override_settings(PRIVATE_DOCUMENT_ROOT=self.root.name);self.settings.enable()
        self.addCleanup(self.settings.disable);self.addCleanup(self.root.cleanup)
        self.client.force_login(self.worker)
    def payload(self,**changes):
        data={'token':str(uuid.uuid4()),'expected_version':1,'performed_date':timezone.localdate().isoformat(),'outcome':'complete','method':'p1','length_m':'3','composition':'1,5+1,5','couplings':[str(self.c1.pk),str(self.c2.pk)]}
        data.update(changes);return data
    def send(self,action='result',data=None,job=None,**changes):
        return self.client.post(reverse('repair_report',args=[(job or self.job).pk,action]),data if data is not None else self.payload(**changes))
    def test_master_sees_only_own_assigned_jobs_and_search_is_scoped(self):
        r=self.client.get(reverse('repairs'));self.assertContains(r,'101.123456');self.assertNotContains(r,'ЗАКРЫТОЕ РНУ')
        for params in [{'q':'ЗАКРЫТЫЙ-ДЕФЕКТ'},{'section':str(self.section2.pk)},{'object':str(self.obj2.pk)}]:self.assertEqual(self.client.get(reverse('repairs'),params).context['page'].paginator.count,0)
        for url in [reverse('repair_detail',args=[self.hidden.pk]),reverse('repair_report',args=[self.hidden.pk,'note'])]:self.assertEqual(self.client.get(url).status_code,404)
    def test_object_access_alone_does_not_open_other_masters_work(self):
        self.hidden.section=self.section;self.hidden.save();self.assertEqual(self.client.get(reverse('repair_detail',args=[self.hidden.pk])).status_code,404)
    def test_revoked_object_assignment_blocks_direct_and_submission(self):
        ObjectAccess.objects.filter(user=self.worker).delete()
        self.assertEqual(self.send().status_code,404);self.assertEqual(Report.objects.count(),0)
    def test_both_leaders_can_view_and_prepare_but_foreman_cannot(self):
        for route in ['repair_new','repair_section_new','repair_import']:
            self.assertEqual(self.client.get(reverse(route)).status_code,403)
            for user in [self.leader,self.second]:self.client.force_login(user);self.assertEqual(self.client.get(reverse(route)).status_code,200)
            self.client.force_login(self.worker)
    def test_get_does_not_start_or_confirm_work(self):
        self.assertEqual(self.client.get(reverse('repair_report',args=[self.job.pk,'start'])).status_code,200)
        self.client.force_login(self.leader);self.assertEqual(self.client.get(reverse('repair_confirm',args=[self.job.pk])).status_code,405)
        self.job.refresh_from_db();self.assertEqual(self.job.state,'planned');self.assertEqual(Report.objects.count(),0)
    def test_result_links_two_defects_and_couplings_without_acceptance(self):
        self.assertEqual(self.send().status_code,302);self.job.refresh_from_db()
        self.assertEqual(self.job.state,'reported');self.assertIsNone(self.job.accepted_date)
        self.assertEqual(self.job.defects.count(),2);self.assertEqual(CouplingUse.objects.filter(job=self.job).count(),2)
        self.assertEqual(len(self.job.reports.get().context['defects']),2)
    def test_partial_keeps_work_open(self):
        self.assertEqual(self.send(outcome='partial').status_code,302);self.job.refresh_from_db();self.assertEqual(self.job.state,'partial')
        self.client.force_login(self.leader);self.client.post(reverse('repair_confirm',args=[self.job.pk]),{'expected_version':2});self.job.refresh_from_db();self.assertEqual(self.job.state,'partial')
    def test_problem_does_not_change_materials_or_claim_completion(self):
        self.assertEqual(self.send('problem',problem='water',note='Котлован затоплен').status_code,302)
        self.job.refresh_from_db();self.assertEqual((self.job.state,self.job.problem,self.job.materials),('planned','water','unknown'))
    def test_resolving_problem_preserves_its_history_and_does_not_close_repair(self):
        self.send('problem',problem='water',note='Вода в котловане')
        self.assertEqual(self.send('resolve',expected_version=2,note='Воду откачали').status_code,302)
        self.job.refresh_from_db();self.assertEqual((self.job.problem,self.job.state),('','planned'));self.assertEqual(self.job.reports.count(),2)
    def test_only_leaders_confirm_current_complete_result(self):
        self.send();url=reverse('repair_confirm',args=[self.job.pk]);self.assertEqual(self.client.post(url,{'expected_version':2}).status_code,403)
        self.client.force_login(self.second);self.client.post(url,{'expected_version':1});self.job.refresh_from_db();self.assertEqual(self.job.state,'reported')
        self.client.post(url,{'expected_version':2});self.job.refresh_from_db();self.assertEqual(self.job.state,'confirmed');self.assertEqual(self.job.reports.get().confirmed_by,self.second)
    def test_retry_is_idempotent_even_with_old_revision(self):
        data=self.payload();self.assertEqual(self.send(data=data).status_code,302);self.assertEqual(self.send(data=data).status_code,302)
        self.assertEqual(Report.objects.count(),1);self.assertEqual(CouplingUse.objects.count(),2)
    def test_stale_different_result_keeps_first_and_returns_conflict(self):
        self.send();r=self.send(note='Устаревшая форма');self.assertEqual(r.status_code,409);self.assertContains(r,'черновик сохранён',status_code=409);self.assertEqual(Report.objects.count(),1)
    def test_reused_token_with_changed_content_never_silently_discards_new_data(self):
        data=self.payload();self.send(data=data);data['note']='Дополненный текст после потери подтверждения'
        r=self.client.post(reverse('repair_report',args=[self.job.pk,'result']),data,HTTP_ACCEPT='application/json')
        self.assertEqual(r.status_code,409);self.assertEqual(r.json()['new_record_version'],2);self.assertEqual(Report.objects.count(),1)
    def test_multipart_file_limit_rejects_entire_report(self):
        files=[SimpleUploadedFile(f'Фото-{i}.txt',b'not private') for i in range(21)]
        r=self.send('note',note='Много файлов',files=files)
        self.assertEqual(r.status_code,413);self.assertEqual(Report.objects.count(),0)
    def test_foreign_or_taken_coupling_cannot_be_used(self):
        for coupling in [self.foreign]:
            self.assertEqual(self.send(couplings=[str(coupling.pk)]).status_code,200);self.assertEqual(Report.objects.count(),0)
        other_job=Job.objects.create(section=self.section,method='p1',created_by=self.leader)
        r=Report.objects.create(job=other_job,author=self.leader,kind='complete',performed_date=timezone.localdate())
        CouplingUse.objects.create(job=other_job,coupling=self.c1,report=r)
        self.assertEqual(self.send().status_code,200);self.assertEqual(self.job.reports.count(),0)
    def test_historical_installations_not_available_as_free_stock(self):
        self.c1.source={'historical_installation':True};self.c1.save()
        available=set(selectable_couplings(self.job).values_list('pk',flat=True));self.assertNotIn(self.c1.pk,available);self.assertIn(self.c2.pk,available)
        self.client.force_login(self.leader)
        r=self.client.get(reverse('repair_report',args=[self.job.pk,'result']));self.assertIn(self.c1,r.context['form'].fields['couplings'].queryset)
    def test_future_date_and_unknown_numbers_need_explicit_explanation(self):
        self.assertEqual(self.send(performed_date=(timezone.localdate()+timedelta(days=1)).isoformat()).status_code,200)
        self.assertEqual(self.send(couplings=[]).status_code,200);self.assertEqual(Report.objects.count(),0)
        self.assertEqual(self.send(couplings=[],note='Номера нужно добавить по паспортам').status_code,302)
    def test_attachment_acl_integrity_and_non_execution(self):
        content=b'%PDF-1.7\nsynthetic\n%%EOF';f=SimpleUploadedFile('паспорт.pdf',content)
        self.assertEqual(self.send(files=f).status_code,302);a=Attachment.objects.get()
        r=self.client.get(reverse('repair_attachment',args=[a.pk]));self.assertEqual(b''.join(r.streaming_content),content);self.assertIn('attachment',r['Content-Disposition']);self.assertEqual(set(r['Cache-Control'].split(', ')),{'private','no-store'})
        self.client.force_login(self.other);self.assertEqual(self.client.get(reverse('repair_attachment',args=[a.pk])).status_code,404)
        self.client.force_login(self.worker);from documents.storage import private_path
        p=private_path(a.storage_key);p.chmod(0o600);p.write_bytes(b'tampered');self.assertEqual(self.client.get(reverse('repair_attachment',args=[a.pk])).status_code,404)
    def test_photo_only_note_and_empty_note_validation(self):
        self.assertEqual(self.send('note',note='').status_code,200)
        self.assertEqual(self.send('note',files=SimpleUploadedFile('фото.jpg',b'\xff\xd8\xffsynthetic')).status_code,302)
        self.job.refresh_from_db();self.assertEqual(self.job.state,'planned')
    def test_confirmed_result_immutable_to_master_and_leader_correction_retains_history(self):
        self.send();old=self.job.reports.get();self.client.force_login(self.leader);self.client.post(reverse('repair_confirm',args=[self.job.pk]),{'expected_version':2})
        self.client.post(reverse('repair_accept',args=[self.job.pk]),{'expected_version':3,'date':timezone.localdate().isoformat(),'basis':'Учебный акт'})
        self.client.force_login(self.worker);self.assertEqual(self.send(expected_version=4).status_code,409);self.assertEqual(self.send('correction',expected_version=4,note='Исправление').status_code,403)
        self.client.force_login(self.leader);self.assertEqual(self.send('correction',expected_version=4,note='Уточнили ошибочно выбранный номер',couplings=[str(self.c2.pk)]).status_code,302)
        self.job.refresh_from_db();old.refresh_from_db();self.assertEqual(self.job.state,'reported');self.assertIsNone(self.job.accepted_date);self.assertIsNotNone(old.confirmed_by)
        self.assertEqual(len(old.coupling_snapshot),2);self.assertEqual(self.job.installed_couplings.count(),1)
    def test_leader_cannot_accept_unconfirmed_or_before_actual_date(self):
        self.send();self.client.force_login(self.leader);url=reverse('repair_accept',args=[self.job.pk])
        self.client.post(url,{'expected_version':2,'date':timezone.localdate().isoformat(),'basis':'Акт'});self.job.refresh_from_db();self.assertIsNone(self.job.accepted_date)
        self.client.post(reverse('repair_confirm',args=[self.job.pk]),{'expected_version':2})
        self.client.post(url,{'expected_version':3,'date':(timezone.localdate()-timedelta(days=1)).isoformat(),'basis':'Акт'});self.job.refresh_from_db();self.assertIsNone(self.job.accepted_date)
    def test_job_creation_combines_defects_without_new_access_to_other_objects(self):
        self.client.force_login(self.leader)
        data={'section':str(self.section.pk),'expected_version':0,'kilometre':'202.222222','defect_numbers':'ДЕМО-А; ДЕМО-Б','method':'p1','length_m':'3','materials':'unknown','assigned_to':str(self.worker.pk)}
        r=self.client.post(reverse('repair_new'),data);self.assertEqual(r.status_code,302);self.assertEqual(Job.objects.get(defects__number='ДЕМО-А').defects.count(),2)
        data['assigned_to']=str(self.other.pk);data['defect_numbers']='ДЕМО-В';r=self.client.post(reverse('repair_new'),data);self.assertEqual(r.status_code,200);self.assertFalse(Defect.objects.filter(number='ДЕМО-В').exists())
    def test_job_edit_stale_and_started_binding_protected(self):
        self.send('start')
        self.client.force_login(self.leader)
        data={'section':str(self.section.pk),'expected_version':1,'kilometre':'101.123456','defect_numbers':'УЧ-01;УЧ-02','method':'p1','length_m':'3','materials':'unknown'}
        url=reverse('repair_edit',args=[self.job.pk]);self.assertContains(self.client.post(url,data),'Работу уже изменили')
        data.update(expected_version=2,defect_numbers='ПОДМЕНА');self.assertContains(self.client.post(url,data),'нельзя заменить привязанные дефекты');self.assertEqual(self.job.defects.count(),2)
    def test_xlsx_separates_side_tables_preserves_merged_defects_and_precision(self):
        parsed=parse_work(workbook(source_rows(),merges=['C7:C8','E7:E8','F7:F8','G7:G8','H7:H8']))
        self.assertEqual(len(parsed['jobs']),2);self.assertEqual(len(parsed['jobs'][0]['defects']),2)
        self.assertEqual(parsed['jobs'][0]['defects'][0]['km'],'101.123456');self.assertEqual(len(parsed['couplings']),2)
        self.assertEqual(parsed['couplings'][0]['serial'],'0007');self.assertTrue(parsed['couplings'][0]['historical_installation'])
        self.assertEqual(parsed['couplings'][1]['raw']['26'],'202.234567')
    def test_xlsx_swapped_columns_external_formulas_and_length_discrepancy(self):
        rows=source_rows();rows[4].update(D='№ секции',E='№ дефекта');rows[7].update(D='СЕК-01',E='ДЕМО-Д1',G=7,H='2+2',C=('[1]Другой!A1','101.123456'))
        parsed=parse_work(workbook(rows));self.assertEqual(parsed['jobs'][0]['defects'][0]['number'],'ДЕМО-Д1');self.assertEqual(len(parsed['warnings']),2)
    def test_xlsx_rejects_malformed_xml_and_non_workbook(self):
        for content in [b'not xlsx',b'PK\x03\x04incomplete']:
            with self.assertRaises(Exception):parse_work(SimpleUploadedFile('bad.xlsx',content))
        f=workbook({1:{'A':'Не ведомость'}})
        with self.assertRaises(Exception):parse_work(f)
    def test_xlsx_extreme_numeric_exponent_does_not_expand_in_memory(self):
        rows=source_rows();rows[7]['C']=('1E999999999','1E999999999')
        parsed=parse_work(workbook(rows));self.assertEqual(len(parsed['jobs']),1);self.assertEqual(parsed['jobs'][0]['defects'][0]['number'],'ДЕМО-Д3')
    def test_numeric_serial_format_preserves_leading_zeroes(self):
        source=workbook(source_rows());buffer=io.BytesIO(source.read())
        with zipfile.ZipFile(buffer) as z:entries={name:z.read(name) for name in z.namelist()}
        sheet=entries['xl/worksheets/sheet1.xml'].decode().replace('<c r="AD7" t="inlineStr"><is><t>0007</t></is></c>','<c r="AD7" s="0"><v>7</v></c>');entries['xl/worksheets/sheet1.xml']=sheet.encode()
        entries['xl/styles.xml']=b'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts><numFmt numFmtId="164" formatCode="0000"/></numFmts><cellXfs><xf numFmtId="164"/></cellXfs></styleSheet>'
        result=io.BytesIO()
        with zipfile.ZipFile(result,'w') as z:
            for name,data in entries.items():z.writestr(name,data)
        parsed=parse_work(SimpleUploadedFile('formatted.xlsx',result.getvalue()));self.assertEqual(parsed['couplings'][0]['serial'],'0007')
    def stage(self):
        self.client.force_login(self.leader)
        r=self.client.post(reverse('repair_import'),{'section':str(self.section.pk),'file':workbook(source_rows(),merges=['C7:C8','E7:E8','F7:F8','G7:G8','H7:H8'])})
        self.assertEqual(r.status_code,302);return ImportBatch.objects.latest('created_at')
    def test_import_preview_is_read_only_then_applies_once_without_completion(self):
        batch=self.stage();url=reverse('repair_import_review',args=[batch.pk]);self.assertEqual(Job.objects.count(),2)
        r=self.client.get(url);self.assertContains(r,'101.123456');self.assertEqual(Job.objects.count(),2)
        data={'row_0':'new','row_1':'new','include_couplings':'on'}
        self.assertEqual(self.client.post(url,data).status_code,302);self.assertEqual(Job.objects.count(),4);self.assertEqual(Coupling.objects.count(),5)
        self.assertEqual(Job.objects.filter(state='planned').count(),4);self.assertEqual(CouplingUse.objects.count(),0)
        self.assertContains(self.client.post(url,data),'уже перенесён');self.assertEqual(Job.objects.count(),4)
    def test_original_workbook_is_preserved_and_only_leaders_can_download(self):
        batch=self.stage();url=reverse('repair_import_source',args=[batch.pk])
        import hashlib
        response=self.client.get(url);self.assertEqual(response.status_code,200);self.assertEqual(hashlib.sha256(b''.join(response.streaming_content)).hexdigest(),batch.sha256)
        self.client.force_login(self.worker);self.assertEqual(self.client.get(url).status_code,403)
    def test_import_new_concurrent_candidate_requires_review(self):
        batch=self.stage();new=Job.objects.create(section=self.section,method='grind',created_by=self.leader)
        d=Defect.objects.create(section=self.section,kilometre=101,number='ДЕМО-Д1');JobDefect.objects.create(job=new,defect=d)
        r=self.client.post(reverse('repair_import_review',args=[batch.pk]),{'row_0':'new','row_1':'skip'})
        self.assertContains(r,'появились совпадения');batch.refresh_from_db();self.assertIsNone(batch.applied_at)
    def test_import_existing_match_does_not_overwrite_confirmed_repair(self):
        d=Defect.objects.create(section=self.section,kilometre=Decimal('101.123456'),number='ДЕМО-Д1');JobDefect.objects.create(job=self.job,defect=d)
        self.job.state='confirmed';self.job.save();batch=self.stage()
        r=self.client.post(reverse('repair_import_review',args=[batch.pk]),{'row_0':str(self.job.pk),'row_1':'skip'})
        self.assertEqual(r.status_code,302);self.job.refresh_from_db();self.assertEqual(self.job.state,'confirmed');self.assertEqual(self.job.reports.get().kind,'imported');self.assertEqual(self.job.method,'p1')
    def test_import_cannot_target_another_section_or_foreign_job(self):
        batch=self.stage();r=self.client.post(reverse('repair_import_review',args=[batch.pk]),{'row_0':str(self.hidden.pk),'row_1':'skip'})
        self.assertEqual(r.status_code,200);batch.refresh_from_db();self.assertIsNone(batch.applied_at)
    def test_filter_pagination_and_invalid_ids_do_not_crash(self):
        for params in [{'section':'broken'},{'object':'broken'},{'q':'NaN'},{'q':'99999999999999999999999'},{'year':'notyear'}]:self.assertEqual(self.client.get(reverse('repairs'),params).status_code,200)
    def test_batch_assignment_restricts_master_and_assigns_atomically(self):
        first=Job.objects.create(section=self.section,method='grind',created_by=self.leader)
        second=Job.objects.create(section=self.section,method='grind',created_by=self.leader)
        self.client.force_login(self.leader);url=reverse('repair_assign_many',args=[self.section.pk])
        form=self.client.get(url).context['form'];snapshot=form.initial['snapshot']
        data={'snapshot':snapshot,'master':str(self.other.pk),'jobs':[str(first.pk),str(second.pk)]}
        self.assertEqual(self.client.post(url,data).status_code,200);first.refresh_from_db();self.assertIsNone(first.assigned_to)
        data['master']=str(self.worker.pk);self.assertEqual(self.client.post(url,data).status_code,302)
        for j in [first,second]:j.refresh_from_db();self.assertEqual(j.assigned_to,self.worker);self.assertEqual(j.version,2)
    def test_batch_stale_job_does_not_change_other_jobs(self):
        first=Job.objects.create(section=self.section,method='grind',created_by=self.leader)
        second=Job.objects.create(section=self.section,method='grind',created_by=self.leader)
        self.client.force_login(self.leader);url=reverse('repair_assign_many',args=[self.section.pk])
        snapshot=self.client.get(url).context['form'].initial['snapshot'];second.version+=1;second.save()
        r=self.client.post(url,{'snapshot':snapshot,'master':str(self.worker.pk),'jobs':[str(first.pk),str(second.pk)]})
        self.assertContains(r,'Назначения не сохранены');first.refresh_from_db();self.assertIsNone(first.assigned_to)
    def test_assignee_lookup_is_leader_only_and_scoped(self):
        url=reverse('repair_assignees',args=[self.section.pk]);self.assertEqual(self.client.get(url).status_code,403)
        self.client.force_login(self.leader);ids={u['id'] for u in self.client.get(url).json()['users']};self.assertIn(str(self.worker.pk),ids);self.assertNotIn(str(self.other.pk),ids)
