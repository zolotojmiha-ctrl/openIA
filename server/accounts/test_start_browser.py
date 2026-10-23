import os
import secrets
import subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from .models import User, AccessGrant
from projects.models import LegalEntity,COMPANY_TAX_ID


class StartBrowserTests(StaticLiveServerTestCase):
    def test_role_screens(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ компания'})
        password=secrets.token_urlsafe(24)
        for i,role in enumerate(['leader','foreman','technician','mechanic','safety','supply'],1):
            user=User.objects.create_user('+7000000080'+str(i),password,full_name='ТЕСТ '+role,role=role if role!='leader' else 'other',leader_slot=1 if role=='leader' else None,must_change_password=False)
            for permission in {'technician':['repairs.prepare','personnel.manage'],'mechanic':['equipment.manage'],'safety':['document.upload'],'supply':['stock.manage']}.get(role,[]):
                AccessGrant.objects.create(user=user,permission=permission,granted_by=user)
        from projects.models import ProjectObject,Contract,ContractObject,ObjectAccess,ContractAccess
        from repairs.models import Section,Job,Defect,JobDefect
        leader=User.objects.get(leader_slot=1);worker=User.objects.get(role='foreman')
        company=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
        obj=ProjectObject.objects.create(legal_entity=company,name='ТЕСТ Омск — Татарск')
        contract=Contract.objects.create(legal_entity=company,number='ТЕСТ-2026',work_kinds=['repairs'])
        ContractObject.objects.create(contract=contract,project_object=obj)
        ObjectAccess.objects.create(user=worker,project_object=obj,granted_by=leader)
        ContractAccess.objects.create(user=worker,contract=contract,granted_by=leader)
        section=Section.objects.create(project_object=obj,rnu='ТЕСТ РНУ',pipeline='ТЕСТ нефтепровод',name='Омск — Татарск')
        for number,state in [('001','planned'),('002','reported')]:
            job=Job.objects.create(contract=contract,section=section,assigned_to=worker,method='grind',state=state,created_by=leader)
            defect=Defect.objects.create(contract=contract,section=section,kilometre='153.546',number=number,pipe_section='140050')
            JobDefect.objects.create(job=job,defect=defect)
        env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password)
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/start-browser.cjs')],env=env,capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
