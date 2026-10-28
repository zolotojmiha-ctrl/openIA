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
from projects.models import LegalEntity,ProjectObject,ObjectAccess,COMPANY_TAX_ID,Contract,ContractObject,ContractAccess
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
        7:{'C':Decimal('101.123'),'D':'2001','E':'1001','F':'П1','G':3,'H':'1,5+1,5','S':46000,'Z':999,'AA':'П1','AB':1500,'AC':10,'AD':'0007','AE':'Учебный склад','AF':46001},
        8:{'D':'2002','Z':('C9','202.235'),'AA':'П2','AB':1000,'AC':8,'AD':'УЧ-08','AE':'Другой учебный склад'},
        9:{'C':Decimal('202.235'),'D':'2003','E':'1002','F':'шлифовка','Y':'Нет доступа'},
        12:{'AA':'На остатках на базе:'},
        15:{'AA':'П1','AB':1000,'AD':99}}

class RepairsTests(TestCase):
    def test_review_notice_for_leaders_only_and_until_review_completed(self):
        self.job.state='reported';self.job.save()
        for leader in (self.leader,self.second):
            self.client.force_login(leader)
            response=self.client.get(reverse('dashboard'))
            self.assertContains(response,'На проверку устранения: 1')
            self.assertContains(response,'href="'+reverse('repairs')+'?state=reported"')
        self.client.force_login(self.worker)
        self.assertNotContains(self.client.get(reverse('dashboard')),'repair-review-notice')
        self.job.state='confirmed';self.job.save()
        self.client.force_login(self.leader)
        self.assertNotContains(self.client.get(reverse('dashboard')),'repair-review-notice')

    def test_register_colors_follow_result_and_reason(self):
        self.client.force_login(self.leader)
        cases=[('reported','swamp','resolved'),('confirmed','','resolved'),
            ('planned','access','blocked'),('planned','water','blocked'),
            ('planned','swamp','blocked'),('planned','winter','blocked'),
            ('planned','materials','materials'),('planned','coupling','materials'),
            ('planned','material_other','materials'),('planned','oversize_p1','materials'),
            ('planned','','neutral'),('planned','not_started','neutral'),
            ('planned','existing_coupling','neutral'),('planned','other','neutral')]
        for state,problem,tone in cases:
            with self.subTest(state=state,problem=problem):
                self.job.state=state;self.job.problem=problem;self.job.save()
                self.assertEqual(self.job.status_tone,tone)
                response=self.client.get(reverse('repairs'))
                self.assertContains(response,'class="repair-tone-'+tone+'"')
                self.assertContains(response,'class="repair-status-label"')

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
        cls.contract=Contract.objects.create(legal_entity=company,number='КОНТРАКТ-ТЕСТ',subject='Устранение дефектов',work_kinds=['repairs'])
        ContractObject.objects.create(contract=cls.contract,project_object=cls.obj)
        for user in (cls.worker,cls.other):ContractAccess.objects.create(user=user,contract=cls.contract,granted_by=cls.leader,show_number=True)
        cls.job=Job.objects.create(contract=cls.contract,section=cls.section,assigned_to=cls.worker,method='p1',length_m=3,composition='1,5+1,5',created_by=cls.leader)
        cls.d1=Defect.objects.create(contract=cls.contract,section=cls.section,kilometre=Decimal('101.123'),number='001',pipe_section='1001')
        cls.d2=Defect.objects.create(contract=cls.contract,section=cls.section,kilometre=Decimal('101.123'),number='002',pipe_section='1001')
        JobDefect.objects.create(job=cls.job,defect=cls.d1);JobDefect.objects.create(job=cls.job,defect=cls.d2)
        cls.hidden=Job.objects.create(section=cls.section2,assigned_to=cls.other,method='grind',created_by=cls.leader)
        d=Defect.objects.create(section=cls.section2,kilometre=999,number='999999');JobDefect.objects.create(job=cls.hidden,defect=d)
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
        r=self.client.get(reverse('repairs'));self.assertContains(r,'101.123');self.assertNotContains(r,'ЗАКРЫТОЕ РНУ')
        for params in [{'q':'999999'},{'section':str(self.section2.pk)},{'object':str(self.obj2.pk)}]:self.assertEqual(self.client.get(reverse('repairs'),params).context['page'].paginator.count,0)
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
        other_job=Job.objects.create(contract=self.contract,section=self.section,method='p1',created_by=self.leader)
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
        data={'contract':str(self.contract.pk),'section':str(self.section.pk),'expected_version':0,'kilometre':'202.222222','defect_numbers':'3001; 3002','method':'p1','length_m':'3','materials':'unknown','assigned_to':str(self.worker.pk)}
        r=self.client.post(reverse('repair_new'),data);self.assertEqual(r.status_code,302);self.assertEqual(Job.objects.get(defects__number='3001').defects.count(),2)
        data['assigned_to']=str(self.other.pk);data['defect_numbers']='3003';r=self.client.post(reverse('repair_new'),data);self.assertEqual(r.status_code,200);self.assertFalse(Defect.objects.filter(number='3003').exists())
    def test_job_edit_stale_and_started_binding_protected(self):
        self.send('start')
        self.client.force_login(self.leader)
        data={'contract':str(self.contract.pk),'section':str(self.section.pk),'expected_version':1,'kilometre':'101.123','defect_numbers':'001;002','method':'p1','length_m':'3','materials':'unknown'}
        url=reverse('repair_edit',args=[self.job.pk]);self.assertContains(self.client.post(url,data),'Работу уже изменили')
        data.update(expected_version=2,defect_numbers='3004');self.assertContains(self.client.post(url,data),'нельзя заменить привязанные дефекты');self.assertEqual(self.job.defects.count(),2)
    def test_xlsx_separates_side_tables_preserves_merged_defects_and_precision(self):
        parsed=parse_work(workbook(source_rows(),merges=['C7:C8','E7:E8','F7:F8','G7:G8','H7:H8']))
        self.assertEqual(len(parsed['jobs']),2);self.assertEqual(len(parsed['jobs'][0]['defects']),2)
        self.assertEqual(parsed['jobs'][0]['defects'][0]['km'],'101.123');self.assertEqual(len(parsed['couplings']),2)
        self.assertEqual(parsed['couplings'][0]['serial'],'0007');self.assertTrue(parsed['couplings'][0]['historical_installation'])
        self.assertEqual(parsed['couplings'][1]['raw']['26'],'202.235')
    def test_xlsx_swapped_columns_external_formulas_and_length_discrepancy(self):
        rows=source_rows();rows[4].update(D='№ секции',E='№ дефекта');rows[7].update(D='1001',E='2001',G=7,H='2+2',C=('[1]Другой!A1','101.123'))
        parsed=parse_work(workbook(rows));self.assertEqual(parsed['jobs'][0]['defects'][0]['number'],'2001');self.assertEqual(len(parsed['warnings']),2)
    def test_xlsx_rejects_malformed_xml_and_non_workbook(self):
        for content in [b'not xlsx',b'PK\x03\x04incomplete']:
            with self.assertRaises(Exception):parse_work(SimpleUploadedFile('bad.xlsx',content))
        f=workbook({1:{'A':'Не ведомость'}})
        with self.assertRaises(Exception):parse_work(f)
    def test_xlsx_extreme_numeric_exponent_does_not_expand_in_memory(self):
        rows=source_rows();rows[7]['C']=('1E999999999','1E999999999')
        parsed=parse_work(workbook(rows));self.assertEqual(len(parsed['jobs']),1);self.assertEqual(parsed['jobs'][0]['defects'][0]['number'],'2003')
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
        r=self.client.post(reverse('repair_import'),{'contract':str(self.contract.pk),'section':str(self.section.pk),'file':workbook(source_rows(),merges=['C7:C8','E7:E8','F7:F8','G7:G8','H7:H8'])})
        self.assertEqual(r.status_code,302);return ImportBatch.objects.latest('created_at')
    def test_import_preview_is_read_only_then_applies_once_without_completion(self):
        batch=self.stage();url=reverse('repair_import_review',args=[batch.pk]);self.assertEqual(Job.objects.count(),2)
        r=self.client.get(url);self.assertContains(r,'101.123');self.assertEqual(Job.objects.count(),2)
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
        batch=self.stage();new=Job.objects.create(contract=self.contract,section=self.section,method='grind',created_by=self.leader)
        d=Defect.objects.create(contract=self.contract,section=self.section,kilometre=Decimal('101.123'),number='2001');JobDefect.objects.create(job=new,defect=d)
        r=self.client.post(reverse('repair_import_review',args=[batch.pk]),{'row_0':'new','row_1':'skip'})
        self.assertContains(r,'появились совпадения');batch.refresh_from_db();self.assertIsNone(batch.applied_at)
    def test_import_existing_match_does_not_overwrite_confirmed_repair(self):
        d=Defect.objects.create(contract=self.contract,section=self.section,kilometre=Decimal('101.123'),number='2001');JobDefect.objects.create(job=self.job,defect=d)
        self.job.state='confirmed';self.job.save();batch=self.stage()
        r=self.client.post(reverse('repair_import_review',args=[batch.pk]),{'row_0':str(self.job.pk),'row_1':'skip'})
        self.assertEqual(r.status_code,302);self.job.refresh_from_db();self.assertEqual(self.job.state,'confirmed');self.assertEqual(self.job.reports.get().kind,'imported');self.assertEqual(self.job.method,'p1')
    def test_import_cannot_target_another_section_or_foreign_job(self):
        batch=self.stage();r=self.client.post(reverse('repair_import_review',args=[batch.pk]),{'row_0':str(self.hidden.pk),'row_1':'skip'})
        self.assertEqual(r.status_code,200);batch.refresh_from_db();self.assertIsNone(batch.applied_at)
    def test_filter_pagination_and_invalid_ids_do_not_crash(self):
        for params in [{'section':'broken'},{'object':'broken'},{'q':'NaN'},{'q':'99999999999999999999999'},{'year':'notyear'}]:self.assertEqual(self.client.get(reverse('repairs'),params).status_code,200)
    def test_batch_assignment_restricts_master_and_assigns_atomically(self):
        first=Job.objects.create(contract=self.contract,section=self.section,method='grind',created_by=self.leader)
        second=Job.objects.create(contract=self.contract,section=self.section,method='grind',created_by=self.leader)
        self.client.force_login(self.leader);url=reverse('repair_assign_many',args=[self.section.pk])
        form=self.client.get(url).context['form'];snapshot=form.initial['snapshot']
        data={'snapshot':snapshot,'master':str(self.other.pk),'jobs':[str(first.pk),str(second.pk)]}
        self.assertEqual(self.client.post(url,data).status_code,200);first.refresh_from_db();self.assertIsNone(first.assigned_to)
        data['master']=str(self.worker.pk);self.assertEqual(self.client.post(url,data).status_code,302)
        for j in [first,second]:j.refresh_from_db();self.assertEqual(j.assigned_to,self.worker);self.assertEqual(j.version,2)
    def test_batch_stale_job_does_not_change_other_jobs(self):
        first=Job.objects.create(contract=self.contract,section=self.section,method='grind',created_by=self.leader)
        second=Job.objects.create(contract=self.contract,section=self.section,method='grind',created_by=self.leader)
        self.client.force_login(self.leader);url=reverse('repair_assign_many',args=[self.section.pk])
        snapshot=self.client.get(url).context['form'].initial['snapshot'];second.version+=1;second.save()
        r=self.client.post(url,{'snapshot':snapshot,'master':str(self.worker.pk),'jobs':[str(first.pk),str(second.pk)]})
        self.assertContains(r,'Назначения не сохранены');first.refresh_from_db();self.assertIsNone(first.assigned_to)
    def test_assignee_lookup_is_leader_only_and_scoped(self):
        url=reverse('repair_assignees',args=[self.section.pk]);self.assertEqual(self.client.get(url).status_code,403)
        self.client.force_login(self.leader);ids={u['id'] for u in self.client.get(url).json()['users']};self.assertIn(str(self.worker.pk),ids);self.assertIn(str(self.other.pk),ids) # Leaders may select a foreman before granting scope explicitly.

    def simple_payload(self, **changes):
        data={'token':str(uuid.uuid4()),'expected_version':self.job.version,'performed_date':timezone.localdate().isoformat()}
        data.update(changes)
        if 'latitude' in data and 'longitude' in data:
            data['coordinates']=str(data.pop('latitude'))+', '+str(data.pop('longitude'))
        return data
    def test_simple_list_has_defect_specification_and_two_actions(self):
        self.job.wall_mm=Decimal('10');self.job.save()
        response=self.client.get(reverse('repairs'))
        for text in ['№ секции','№ дефекта','Метод ремонта','Толщина стенки','1001','001','002','10','Устранён','Не устранён']:
            self.assertContains(response,text)
        self.assertNotContains(response,'999999')
    def test_simple_result_requires_coordinates_and_uses_plan_not_posted_engineering(self):
        response=self.send('repaired',self.simple_payload())
        self.assertIn('coordinates',response.context['form'].errors)
        self.assertEqual(Report.objects.count(),0)
        response=self.send('repaired',self.simple_payload(latitude='56.123456',longitude='73.123456',method='cut',length_m='999',outcome='partial'))
        self.assertEqual(response.status_code,302)
        report=Report.objects.get(job=self.job)
        self.assertEqual(report.kind,'complete');self.assertEqual(report.method,'p1');self.assertEqual(report.length_m,3)
        self.assertEqual(report.latitude,Decimal('56.123456'));self.assertEqual(report.attachments.count(),0)
        self.job.refresh_from_db();self.assertEqual(self.job.state,'reported')
    def test_simple_problem_reasons_and_required_explanations(self):
        for reason in ['swamp','coupling','material_other','other']:
            with self.subTest(reason=reason):
                data=self.simple_payload(problem=reason)
                if reason in ('material_other','other'):
                    response=self.send('unrepaired',data)
                    self.assertIn('note',response.context['form'].errors)
                    data['note']='Нет изоляционной ленты' if reason=='material_other' else 'Перекрыта дорога'
                self.assertEqual(self.send('unrepaired',data).status_code,302)
                self.job.refresh_from_db();self.assertEqual(self.job.problem,reason)
        self.assertEqual(self.job.reports.count(),4)
    def test_simple_actions_preserve_access_confirmed_lock_history_and_retry(self):
        for action in ['repaired','unrepaired']:
            self.assertEqual(self.client.get(reverse('repair_report',args=[self.hidden.pk,action])).status_code,404)
        data=self.simple_payload(latitude='56',longitude='73')
        self.assertEqual(self.send('repaired',data).status_code,302)
        self.assertEqual(self.send('repaired',data).status_code,302)
        self.assertEqual(self.job.reports.count(),1)
        self.job.refresh_from_db()
        self.assertEqual(self.send('unrepaired',self.simple_payload(problem='swamp')).status_code,302)
        self.assertEqual(self.job.reports.count(),2)
        self.job.refresh_from_db();self.assertEqual(self.job.state,'progress')
        from .services import confirm_work
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):confirm_work(self.leader,self.job.pk,self.job.version)
        self.job.refresh_from_db();self.job.state='confirmed';self.job.save()
        self.assertEqual(self.send('unrepaired',self.simple_payload(problem='swamp')).status_code,409)
        self.assertEqual(self.job.reports.count(),2)
    def test_simple_missing_plan_length_returns_error_without_creating_result(self):
        self.job.length_m=None;self.job.save()
        response=self.send('repaired',self.simple_payload(latitude='56',longitude='73'))
        self.assertContains(response,'Руководитель должен уточнить задание')
        self.assertEqual(Report.objects.count(),0)

    def test_job_wall_thickness_accepts_positive_or_unknown_only(self):
        from .forms import JobForm
        data={'contract':self.contract.pk,'section':self.section.pk,'kilometre':'101.1','defect_numbers':'6001','method':'p1','length_m':'3','materials':'unknown','expected_version':0}
        for value in ['0','-1','10.25']:
            form=JobForm(data={**data,'wall_mm':value},user=self.leader)
            self.assertFalse(form.is_valid());self.assertIn('wall_mm',form.errors)
        for value in ['','10']:
            form=JobForm(data={**data,'wall_mm':value},user=self.leader)
            self.assertTrue(form.is_valid(),form.errors)
    def standard_file(self,rows):
        from .excel import workbook as make_workbook
        return SimpleUploadedFile('Дефекты.xlsx',make_workbook(rows))
    def standard_rows(self):
        return [['Р-01','101.123','1001','4001','П1','3','10',self.worker.phone],
                ['Р-01','101.123','1001','4002','П1','3','10',self.worker.phone],
                ['Р-02','202.235','1002','4003','П2','1.5','8',self.worker.phone]]
    def test_paste_coordinates_from_maps_and_validate_range(self):
        from .forms import ReportForm
        for value,valid in [('56.12345678, 73.12345678',True),('-56.123456; -73.123456',True),('56,123456;73,123456',True),('56.123456 73.123456',True),('91, 73',False),('56, 181',False),('https://maps.google.com/point',False),('56',False)]:
            form=ReportForm(data=self.simple_payload(coordinates=value),job=self.job,action='repaired',user=self.worker)
            self.assertEqual(form.is_valid(),valid,(value,form.errors))
    def test_standard_excel_groups_and_rejects_different_km_or_conflicting_sizes(self):
        rows=self.standard_rows();parsed=parse_work(self.standard_file(rows))
        self.assertEqual(len(parsed['jobs']),2);self.assertEqual(len(parsed['jobs'][0]['defects']),2)
        self.assertEqual(Decimal(parsed['jobs'][0]['wall_mm']),Decimal('10'))
        for column,value in [(1,'202'),(4,'П2'),(5,'4'),(6,'8'),(7,self.other.phone)]:
            changed=[list(row) for row in rows];changed[1][column]=value
            with self.assertRaisesMessage(Exception,'различаются'):parse_work(self.standard_file(changed))
        changed=[list(row) for row in rows];changed[2][1]='101.123';changed[2][3]='4001'
        with self.assertRaisesMessage(Exception,'повторяется'):parse_work(self.standard_file(changed))
    def test_standard_import_assigns_two_separate_repairs_with_independent_results(self):
        self.client.force_login(self.leader)
        response=self.client.post(reverse('repair_import'),{'contract':str(self.contract.pk),'section':str(self.section.pk),'file':self.standard_file(self.standard_rows())})
        self.assertEqual(response.status_code,302)
        batch=ImportBatch.objects.latest('created_at')
        self.assertEqual(self.client.post(reverse('repair_import_review',args=[batch.pk]),{'row_0':'new','row_1':'new'}).status_code,302)
        jobs=list(Job.objects.filter(source__batch=str(batch.pk)).order_by('source__repair_code'))
        self.assertEqual(len(jobs),2);self.assertEqual(jobs[0].defects.count(),2);self.assertEqual(jobs[1].defects.count(),1)
        self.assertTrue(all(j.assigned_to_id==self.worker.pk for j in jobs))
        self.assertEqual(jobs[0].wall_mm,Decimal('10'))
        self.client.force_login(self.worker)
        for job,coordinates in zip(jobs,['56.1, 73.1','57.2, 74.2']):
            data=self.simple_payload(coordinates=coordinates);data['expected_version']=job.version
            self.assertEqual(self.send('repaired',data,job=job).status_code,302)
        self.assertEqual(jobs[0].reports.get().latitude,Decimal('56.1'))
        self.assertEqual(jobs[1].reports.get().latitude,Decimal('57.2'))
        self.assertEqual(self.job.reports.count(),0)
    def test_export_is_real_xlsx_scoped_and_roundtrips_groups(self):
        response=self.client.get(reverse('repair_excel_export'))
        self.assertEqual(response.status_code,200)
        self.assertIn('spreadsheetml',response['Content-Type'])
        parsed=parse_work(SimpleUploadedFile('export.xlsx',response.content))
        self.assertEqual(len(parsed['jobs']),1);self.assertEqual(len(parsed['jobs'][0]['defects']),2)
        self.assertEqual(parsed['jobs'][0]['master_phone'],self.worker.phone)
        self.assertNotIn('ЗАКРЫТЫЙ',str(parsed))
        self.assertEqual(self.client.get(reverse('repair_excel_export'),{'section':self.section2.pk}).status_code,200)
        # The inaccessible section produces a header-only workbook, no private rows.
        from .importer import read_book
        book=read_book(SimpleUploadedFile('empty.xlsx',self.client.get(reverse('repair_excel_export'),{'section':self.section2.pk}).content))
        self.assertTrue(all(r==1 for r,c in book['cells']))
    def test_preparation_grant_scopes_technician_without_granting_reports_or_confirmation(self):
        from accounts.models import AccessGrant
        AccessGrant.objects.create(user=self.other,permission='repairs.prepare',granted_by=self.leader)
        ObjectAccess.objects.create(user=self.other,project_object=self.obj,granted_by=self.leader)
        self.client.force_login(self.other)
        for route in ['repair_import','repair_new','repair_excel_template']:
            self.assertEqual(self.client.get(reverse(route)).status_code,200)
        self.assertContains(self.client.get(reverse('repairs')),'101.123')
        self.assertEqual(self.client.get(reverse('repair_detail',args=[self.hidden.pk])).status_code,404)
        self.assertEqual(self.send('repaired',self.simple_payload(coordinates='56, 73')).status_code,404)
        self.assertEqual(self.client.post(reverse('repair_confirm',args=[self.job.pk]),{'expected_version':1}).status_code,403)
        AccessGrant.objects.filter(user=self.other).delete()
        self.assertEqual(self.client.get(reverse('repair_import')).status_code,403)
    def test_import_rejects_unknown_master_wrong_section_and_revoked_master_access(self):
        self.client.force_login(self.leader);rows=self.standard_rows();rows[0][7]=rows[1][7]=self.other.phone
        self.assertContains(self.client.post(reverse('repair_import'),{'contract':self.contract.pk,'section':self.section.pk,'file':self.standard_file(rows)}),'не имеет доступа')
        rows=self.standard_rows()
        for row in rows:row.append(str(self.section2.pk))
        self.assertContains(self.client.post(reverse('repair_import'),{'contract':self.contract.pk,'section':self.section.pk,'file':self.standard_file(rows)}),'другого участка')
        self.client.post(reverse('repair_import'),{'contract':self.contract.pk,'section':self.section.pk,'file':self.standard_file(self.standard_rows())})
        batch=ImportBatch.objects.latest('created_at');ObjectAccess.objects.filter(user=self.worker).delete()
        response=self.client.post(reverse('repair_import_review',args=[batch.pk]),{'row_0':'new','row_1':'new'})
        self.assertContains(response,'Доступ выбранного мастера изменился')
        self.assertEqual(Job.objects.filter(source__batch=str(batch.pk)).count(),0)
    def test_simple_result_rejects_legacy_group_spanning_different_km(self):
        self.d2.kilometre=202;self.d2.save()
        response=self.send('repaired',self.simple_payload(coordinates='56, 73'))
        self.assertContains(response,'разделить задание')
        self.assertEqual(self.job.reports.count(),0)

    def test_standard_import_matches_number_and_km_not_number_alone(self):
        from .import_views import matches
        item={'code':'Р-NEW','defects':[{'number':self.d1.number,'km':'202','pipe_section':'1002'}]}
        self.assertEqual(matches(self.section,item,self.contract).count(),0)
        item['defects'][0]['km']=self.d1.km
        self.assertEqual(matches(self.section,item,self.contract).get().pk,self.job.pk)
    def test_technician_cannot_import_inventory_or_duplicate_existing_defects(self):
        from accounts.models import AccessGrant
        AccessGrant.objects.create(user=self.other,permission='repairs.prepare',granted_by=self.leader)
        ObjectAccess.objects.create(user=self.other,project_object=self.obj,granted_by=self.leader)
        self.client.force_login(self.other)
        self.client.post(reverse('repair_import'),{'contract':self.contract.pk,'section':self.section.pk,'file':workbook(source_rows(),['C7:C8','F7:F8','G7:G8','H7:H8'])})
        batch=ImportBatch.objects.latest('created_at');count=Coupling.objects.count()
        response=self.client.post(reverse('repair_import_review',args=[batch.pk]),{'row_0':'new','row_1':'new','include_couplings':'on'})
        self.assertEqual(response.status_code,302);self.assertEqual(Coupling.objects.count(),count)
        rows=self.standard_rows();rows[0][3]=self.d1.number
        self.client.post(reverse('repair_import'),{'contract':self.contract.pk,'section':self.section.pk,'file':self.standard_file(rows)})
        batch=ImportBatch.objects.latest('created_at')
        response=self.client.post(reverse('repair_import_review',args=[batch.pk]),{'row_0':'new','row_1':'new'})
        self.assertContains(response,'Дефект уже внесён')
        self.assertFalse(Job.objects.filter(source__batch=str(batch.pk)).exists())

    def test_export_preserves_object_and_master_filters(self):
        from .importer import read_book
        for params in [{'object':str(self.obj2.pk)},{'master':str(self.other.pk)}]:
            response=self.client.get(reverse('repair_excel_export'),params)
            self.assertEqual(response.status_code,200)
            book=read_book(SimpleUploadedFile('scoped.xlsx',response.content))
            self.assertTrue(all(r==1 for r,c in book['cells']))

    def test_standard_excel_accepts_serialization_noise_without_losing_real_precision(self):
        rows=self.standard_rows()
        for row in rows[:2]:
            row[1]='101.12345599999999';row[5]='2.9999999999999996';row[6]='9.999999999999998'
        parsed=parse_work(self.standard_file(rows))
        first=parsed['jobs'][0]
        self.assertEqual(Decimal(first['defects'][0]['km']),Decimal('101.123'))
        self.assertEqual(Decimal(first['length_m']),Decimal('3'))
        self.assertEqual(Decimal(first['wall_mm']),Decimal('10'))
        for column,value in [(1,'101.1237'),(5,'3.0001'),(6,'10.251')]:
            changed=[list(r) for r in rows];changed[0][column]=value
            with self.assertRaises(Exception):parse_work(self.standard_file(changed))
    def test_standard_excel_reports_missing_code_and_bad_method_separately(self):
        rows=self.standard_rows();rows[0][0]=''
        with self.assertRaisesMessage(Exception,'не заполнен «Код ремонта»'):parse_work(self.standard_file(rows))
        rows=self.standard_rows();rows[0][4]='П99'
        with self.assertRaisesMessage(Exception,'неизвестный метод ремонта'):parse_work(self.standard_file(rows))
        rows=self.standard_rows();rows[0][1]='не число'
        with self.assertRaisesMessage(Exception,'некорректный километр'):parse_work(self.standard_file(rows))
    def visibility_token(self,defect=None,restore=False):
        route='repair_defect_restore' if restore else 'repair_defect_delete'
        response=self.client.get(reverse(route,args=[(defect or self.d1).pk]))
        self.assertEqual(response.status_code,200)
        return response.context['snapshot']
    def test_delete_defect_is_reversible_and_preserves_reports_photos_and_group(self):
        self.assertEqual(self.send('repaired',self.simple_payload(coordinates='56, 73')).status_code,302)
        report=self.job.reports.get();old_context=report.context
        self.client.force_login(self.leader)
        token=self.visibility_token()
        self.assertEqual(self.client.post(reverse('repair_defect_delete',args=[self.d1.pk]),{'snapshot':token}).status_code,302)
        self.d1.refresh_from_db();self.assertIsNotNone(self.d1.archived_at)
        self.assertEqual(self.job.defects.count(),2);self.assertEqual(self.job.reports.count(),1)
        report.refresh_from_db();self.assertEqual(report.context,old_context)
        response=self.client.get(reverse('repairs'));self.assertNotIn(self.d1.pk,[r['defect'].pk for g in response.context['groups'] for r in g['rows']]);self.assertContains(response,self.d2.number)
        self.assertContains(self.client.get(reverse('repairs'),{'archive':'1'}),self.d1.number)
        from .importer import read_book
        exported=self.client.get(reverse('repair_excel_export'))
        book=read_book(SimpleUploadedFile('export.xlsx',exported.content))
        values=list(book['cells'].values());self.assertNotIn(self.d1.number,values);self.assertIn(self.d2.number,values)
        token=self.visibility_token(restore=True)
        self.assertEqual(self.client.post(reverse('repair_defect_restore',args=[self.d1.pk]),{'snapshot':token}).status_code,302)
        self.d1.refresh_from_db();self.assertIsNone(self.d1.archived_at)
        self.assertContains(self.client.get(reverse('repairs')),self.d1.number)
    def test_delete_requires_preparation_permission_object_access_and_confirmation(self):
        url=reverse('repair_defect_delete',args=[self.d1.pk])
        self.assertEqual(self.client.get(url).status_code,403)
        self.client.force_login(self.leader)
        self.visibility_token();self.d1.refresh_from_db();self.assertIsNone(self.d1.archived_at)
        self.assertEqual(self.client.post(url,{}).status_code,409)
        from accounts.models import AccessGrant
        AccessGrant.objects.create(user=self.other,permission='repairs.prepare',granted_by=self.leader)
        self.client.force_login(self.other);self.assertEqual(self.client.get(url).status_code,404)
        ObjectAccess.objects.create(user=self.other,project_object=self.obj,granted_by=self.leader)
        token=self.visibility_token();self.assertEqual(self.client.post(url,{'snapshot':token}).status_code,302)
    def test_deleting_last_defect_hides_job_and_invalidates_old_master_draft(self):
        stale=self.simple_payload(coordinates='56, 73')
        self.client.force_login(self.leader)
        token=self.visibility_token()
        self.client.post(reverse('repair_defect_delete',args=[self.d1.pk]),{'snapshot':token})
        self.client.force_login(self.worker)
        self.assertEqual(self.send('repaired',stale).status_code,409)
        self.client.force_login(self.leader);token=self.visibility_token(self.d2)
        self.client.post(reverse('repair_defect_delete',args=[self.d2.pk]),{'snapshot':token})
        self.assertEqual(self.client.get(reverse('repairs')).context['page'].paginator.count,1) # unrelated visible job
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse('repair_detail',args=[self.job.pk])).status_code,404)
        self.assertEqual(self.send('repaired',stale).status_code,404)
        self.assertEqual(self.job.reports.count(),0)
    def test_stale_delete_confirmation_does_not_hide_changed_work(self):
        self.client.force_login(self.leader);token=self.visibility_token()
        self.job.version+=1;self.job.save()
        response=self.client.post(reverse('repair_defect_delete',args=[self.d1.pk]),{'snapshot':token})
        self.assertEqual(response.status_code,409);self.d1.refresh_from_db();self.assertIsNone(self.d1.archived_at)
    def test_register_is_one_compact_table_with_defects_sorted_by_kilometre(self):
        response=self.client.get(reverse('repairs'))
        groups=response.context['groups'];self.assertEqual(len(groups),1)
        self.assertEqual([row['number'] for row in groups[0]['rows']],[1,2])
        html=response.content.decode()
        self.assertLess(html.index('>№<'),html.index('>Км<'))
        self.assertLess(html.index('>№ дефекта<'),html.index('>№ секции<'))
        self.assertNotIn('class="repair-card"',html)

    def test_manual_creation_cannot_silently_reuse_removed_defect(self):
        self.d1.archived_at=timezone.now();self.d1.save()
        self.client.force_login(self.leader)
        before=Job.objects.count()
        response=self.client.post(reverse('repair_new'),{'contract':self.contract.pk,'section':self.section.pk,'kilometre':str(self.d1.kilometre),'defect_numbers':self.d1.number,'method':'p1','length_m':'3','materials':'unknown','expected_version':0})
        self.assertContains(response,'Восстановите его')
        self.assertEqual(Job.objects.count(),before)
    def test_kilometres_round_half_up_to_three_decimals_for_import_and_display(self):
        from .values import kilometre
        for raw,expected in [('153.568499999','153.568'),('153.5685','153.569'),('1.2345','1.235'),('1.2','1.200'),('0.0005','0.001')]:
            self.assertEqual(format(kilometre(raw),'.3f'),expected)
        rows=self.standard_rows()
        rows[0][1]=rows[1][1]='101.1235000000001'
        result=parse_work(self.standard_file(rows))
        self.assertEqual(result['jobs'][0]['defects'][0]['km'],'101.124')
        self.d1.kilometre=Decimal('101.123500');self.assertEqual(self.d1.km,'101.124')
    def test_numeric_identifiers_preserve_zeros_and_reject_letters_and_decimal_ids(self):
        rows=self.standard_rows();rows[0][2]='00012';rows[0][3]='000001'
        parsed=parse_work(self.standard_file(rows));self.assertEqual(parsed['jobs'][0]['defects'][0]['number'],'000001')
        self.assertEqual(parsed['jobs'][0]['defects'][0]['pipe_section'],'00012')
        for column,value in [(2,'СЕК-1'),(3,'Д-1'),(3,'1.0'),(3,'-1')]:
            changed=self.standard_rows();changed[0][column]=value
            with self.assertRaisesMessage(Exception,'только цифры'):parse_work(self.standard_file(changed))
        rows=self.standard_rows();rows[0][6]=rows[1][6]='8.5'
        with self.assertRaises(Exception):parse_work(self.standard_file(rows))
        rows=self.standard_rows();rows[0][4]=rows[1][4]='Шлифовка';rows[0][6]=rows[1][6]=''
        self.assertEqual(parse_work(self.standard_file(rows))['jobs'][0]['method'],'grind')
    def test_rounding_reuses_legacy_identity_without_rewriting_old_evidence(self):
        from .values import get_or_create_defect
        old=Defect.objects.create(contract=self.contract,section=self.section,kilometre=Decimal('154.123456'),number='00012',pipe_section='0002')
        found,created=get_or_create_defect(contract=self.contract,section=self.section,kilometre=Decimal('154.123'),number='00012',defaults={'pipe_section':'0002'})
        self.assertFalse(created);self.assertEqual(found.pk,old.pk)
        old.refresh_from_db();self.assertEqual(old.kilometre,Decimal('154.123456'));self.assertEqual(old.km,'154.123')
        rows=self.standard_rows();rows[0][1]='101.1231';rows[1][1]='101.1234';rows[1][3]=rows[0][3]
        with self.assertRaisesMessage(Exception,'повторяется'):parse_work(self.standard_file(rows))
    def test_previous_coupling_and_missing_oversize_p1_stay_unrepaired(self):
        for reason in ['existing_coupling','oversize_p1']:
            data=self.simple_payload(problem=reason)
            response=self.send('unrepaired',data);self.assertIn('note',response.context['form'].errors)
            data['note']='Обнаружена ранее установленная П2. Нужна П1 увеличенного диаметра; на складе нет.'
            self.assertEqual(self.send('unrepaired',data).status_code,302)
            self.job.refresh_from_db();self.assertEqual(self.job.defect_status,'Не устранён');self.assertEqual(self.job.method,'p1')
        self.assertEqual(CouplingUse.objects.count(),0)
        self.assertEqual(self.job.reports.filter(kind='complete').count(),0)
        self.assertEqual(self.job.reports.filter(kind='problem').count(),2)
    def test_reassignment_preserves_authors_and_transfers_only_current_work_access(self):
        self.assertEqual(self.send('unrepaired',self.simple_payload(problem='winter')).status_code,302)
        previous=self.job.reports.get()
        ObjectAccess.objects.create(user=self.other,project_object=self.obj,granted_by=self.leader)
        self.job.refresh_from_db();self.client.force_login(self.leader)
        url=reverse('repair_reassign',args=[self.job.pk])
        response=self.client.post(url,{'expected_version':self.job.version,'master':self.other.pk})
        self.assertEqual(response.status_code,302);self.job.refresh_from_db();self.assertEqual(self.job.assigned_to,self.other)
        previous.refresh_from_db();self.assertEqual(previous.author,self.worker)
        self.assertEqual(self.job.reports.get(kind='assignment').author,self.leader)
        self.client.force_login(self.worker);self.assertEqual(self.client.get(reverse('repair_detail',args=[self.job.pk])).status_code,404)
        self.client.force_login(self.other);self.assertEqual(self.client.get(reverse('repair_detail',args=[self.job.pk])).status_code,200)
        self.assertEqual(self.job.problem,'winter')
    def test_reassignment_requires_preparation_permission_and_rejects_stale_or_closed_job(self):
        url=reverse('repair_reassign',args=[self.job.pk]);self.assertEqual(self.client.get(url).status_code,403)
        self.client.force_login(self.leader)
        response=self.client.post(url,{'expected_version':1,'master':self.other.pk})
        self.assertIn('master',response.context['form'].errors)
        self.job.version+=1;self.job.save()
        self.assertEqual(self.client.post(url,{'expected_version':1,'master':self.worker.pk}).status_code,409)
        self.job.state='confirmed';self.job.save()
        self.assertEqual(self.client.post(url,{'expected_version':2,'master':self.worker.pk}).status_code,409)
        self.assertEqual(self.job.reports.count(),0)
