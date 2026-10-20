import os
import secrets
import subprocess
import tempfile
from decimal import Decimal
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from accounts.models import User,AccessGrant
from projects.models import LegalEntity,ProjectObject,ObjectAccess,COMPANY_TAX_ID
from .models import Section,Job,Defect,JobDefect,Coupling,Report,Attachment

class RepairBrowserAcceptance(StaticLiveServerTestCase):
    def test_phone_workflow_drafts_photos_and_leader_review(self):
        password=secrets.token_urlsafe(24)
        leader=User.objects.create_user('+70000000001',password,full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        worker=User.objects.create_user('+70000000003',password,full_name='ТЕСТ Мастер',role='foreman',must_change_password=False)
        company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ Компания'})[0]
        obj=ProjectObject.objects.create(legal_entity=company,name='Учебный объект')
        ObjectAccess.objects.create(project_object=obj,user=worker,granted_by=leader)
        section=Section.objects.create(project_object=obj,rnu='Учебное РНУ',pipeline='Учебный трубопровод',name='Учебный участок')
        job=Job.objects.create(section=section,method='p1',length_m=3,wall_mm=Decimal('10.25'),source={'repair_code':'Р-01'},composition='1,5+1,5',assigned_to=worker,created_by=leader)
        for number in ['ДЕМО-01','ДЕМО-02']:
            d=Defect.objects.create(section=section,number=number,kilometre=Decimal('101.123456'),pipe_section='СЕК-01');JobDefect.objects.create(job=job,defect=d)
        for serial in ['УЧЕБНАЯ-001','УЧЕБНАЯ-002']:Coupling.objects.create(project_object=obj,method='p1',serial=serial,length_mm=1500,warehouse='Учебный склад')
        technician=User.objects.create_user('+70000000004',password,full_name='ТЕСТ Техник',role='as_built',must_change_password=False)
        ObjectAccess.objects.create(project_object=obj,user=technician,granted_by=leader)
        AccessGrant.objects.create(user=technician,permission='repairs.prepare',granted_by=leader)
        env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password,ARMADA_REPAIR_ID=str(job.pk),ARMADA_SECTION_ID=str(section.pk))
        with tempfile.TemporaryDirectory(prefix='armada-field-files-') as root,override_settings(PRIVATE_DOCUMENT_ROOT=root):
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/repairs-browser.cjs')],env=env,capture_output=True,text=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            job.refresh_from_db();self.assertEqual(job.state,'confirmed');self.assertIsNotNone(job.accepted_date)
            self.assertEqual(job.defects.count(),2);self.assertEqual(job.installed_couplings.count(),2)
            self.assertEqual(Attachment.objects.filter(report__job=job).count(),1)
            self.assertEqual(Report.objects.filter(job=job,kind='note').count(),1)
            from .excel import workbook
            from pathlib import Path
            sample=Path(root)/'defects.xlsx'
            sample.write_bytes(workbook([
                ['Р-02','202.234567','СЕК-02','ДЕМО-XL03','П2','1.5','8',worker.phone],
                ['Р-02','202.234567','СЕК-02','ДЕМО-XL04','П2','1.5','8',worker.phone],
                ['Р-03','303.345678','СЕК-03','ДЕМО-XL05','П3','2','10',worker.phone],
            ]))
            env['ARMADA_SAMPLE_FILE']=str(sample)
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/repairs-excel-browser.cjs')],env=env,capture_output=True,text=True,timeout=120)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            first=Job.objects.get(source__repair_code='Р-02');second=Job.objects.get(source__repair_code='Р-03')
            self.assertEqual(first.reports.get().latitude,Decimal('56.900001'))
            self.assertEqual(first.defects.count(),2);self.assertEqual(first.assigned_to_id,worker.pk)
            self.assertFalse(second.reports.exists());self.assertEqual(second.state,'planned')
