import os,secrets,subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.utils import timezone
from accounts.models import User
from documents.models import Person
from repairs.models import Coupling,Section,Job,Defect,JobDefect,CouplingUse
from .models import LegalEntity,ProjectObject,ObjectAccess,Contract,ContractObject,ContractAccess,StockItem,StockBalance,COMPANY_TAX_ID
from .crews import create_crew,assign_crew_place
from . import stock

class StockBrowserTests(StaticLiveServerTestCase):
    def test_materials_and_issued_coupling_in_real_ui(self):
        import uuid
        password=secrets.token_urlsafe(24)
        leader=User.objects.create_user('+70000000001',password,leader_slot=1,full_name='ТЕСТ Руководитель',must_change_password=False)
        worker=User.objects.create_user('+70000000002',password,role='foreman',full_name='ТЕСТ Мастер',must_change_password=False)
        company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})[0];obj=ProjectObject.objects.create(legal_entity=company,name='ТЕСТ Омск–Татарск')
        contract=Contract.objects.create(legal_entity=company,number='ТЕСТ-1726',subject='Дефекты',work_kinds=['repairs']);ContractObject.objects.create(contract=contract,project_object=obj)
        ObjectAccess.objects.create(user=worker,project_object=obj,granted_by=leader);ContractAccess.objects.create(user=worker,contract=contract,granted_by=leader)
        p=Person.objects.create(legal_entity=company,full_name=worker.full_name,user=worker,created_by=leader);b=create_crew(leader,'ТЕСТ Бригада №1',p,[],timezone.localdate());crew=assign_crew_place(leader,b.pk,b.version,contract.pk,obj.pk,timezone.localdate(),None)
        section=Section.objects.create(project_object=obj,rnu='ТЕСТ',pipeline='ТЕСТ',name='Омск–Татарск');job=Job.objects.create(contract=contract,section=section,method='p2',length_m=1,wall_mm=8,assigned_to=worker,crew_assignment=crew,created_by=leader);d=Defect.objects.create(contract=contract,section=section,kilometre='153.546',number='0001',pipe_section='130001');JobDefect.objects.create(job=job,defect=d)
        c=Coupling.objects.create(project_object=obj,method='p2',length_mm=1000,wall_mm=8,serial='ТЕСТ-001')
        unit=stock.save_item(leader,None,None,dict(sku='М-001',name='ТЕСТ Муфта П2 №001',kind='coupling',unit='pcs',method='p2',diameter_mm=720,length_mm=1000,wall_mm=8,archived=False,coupling=c))
        a=stock.save_warehouse(leader,None,None,{'name':'ТЕСТ Центральный'});hold=stock.save_warehouse(leader,None,None,{'name':'ТЕСТ Выдано бригаде №1','brigade_assignment':crew})
        stock.move(leader,unit.pk,'receipt',1,None,a.pk,timezone.localdate(),'Приход',uuid.uuid4());stock.move(leader,unit.pk,'issue',1,a.pk,hold.pk,timezone.localdate(),'Выдано на дефект 0001',uuid.uuid4(),job.pk)
        env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password,ARMADA_STOCK_JOB=str(job.pk),ARMADA_HOLDING_ID=str(hold.pk))
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/stock-browser.cjs')],env=env,capture_output=True,text=True,timeout=180)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr);self.assertTrue(CouplingUse.objects.filter(coupling=c,job=job).exists());self.assertEqual(StockBalance.objects.get(warehouse=hold,item=unit).quantity,0)
