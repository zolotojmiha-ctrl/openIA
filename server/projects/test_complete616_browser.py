import os,json,subprocess,tempfile,hashlib,uuid
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.conf import settings
from accounts.models import User
from documents.models import Person,Document,DocumentVersion
from documents.storage import private_path
from .models import LegalEntity,COMPANY_TAX_ID,Contract,ProjectObject,ContractObject,Brigade,Equipment,Admission,EquipmentCustody,CustomerAccess,MaterialRequest
from repairs.models import Section,Job,Defect,JobDefect

class CompleteWorkflowBrowser(StaticLiveServerTestCase):
    def test_operations_and_customer_boundary_in_real_browser(self):
        with tempfile.TemporaryDirectory() as tmp,override_settings(PRIVATE_DOCUMENT_ROOT=tmp):
            entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
            leader=User.objects.create_user('+79999981671','123456',leader_slot=1,full_name='ТЕСТ Руководитель',must_change_password=False)
            customer=User.objects.create_user('+79999981672','123456',role='customer',full_name='ТЕСТ Заказчик',must_change_password=False)
            contract=Contract.objects.create(legal_entity=entity,number='ТЕСТ-КОНТРАКТ-616',subject='Дефекты',private_notes='СЕКРЕТ-ФИНАНСОВ',status='active',work_kinds=['repairs'])
            area=ProjectObject.objects.create(legal_entity=entity,name='ТЕСТ-УЧАСТОК',status='active');ContractObject.objects.create(contract=contract,project_object=area)
            master=Person.objects.create(legal_entity=entity,full_name='ТЕСТ Мастер',created_by=leader)
            worker=Person.objects.create(legal_entity=entity,full_name='ТЕСТ Водитель',created_by=leader)
            crew=Brigade.objects.create(legal_entity=entity,name='ТЕСТ Бригада',foreman=master,created_by=leader)
            equipment=Equipment.objects.create(legal_entity=entity,name='ТЕСТ Соболь',category='Авто',inventory_number='616-BROWSER',created_by=leader)
            doc=Document.objects.create(legal_entity=entity,title='ТЕСТ ППР',section='general',created_by=leader)
            key='ab/'+uuid.uuid4().hex;path=private_path(key);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'synthetic ppr')
            version=DocumentVersion.objects.create(document=doc,sequence=1,storage_key=key,original_name='ppr.txt',content_type='text/plain',size=13,sha256=hashlib.sha256(b'synthetic ppr').hexdigest(),uploaded_by=leader);doc.current_version=version;doc.save();doc.personnel_linked.add(worker)
            section=Section.objects.create(project_object=area,rnu='ТЕСТ РНУ',pipeline='ОИ',name='ТЕСТ')
            job=Job.objects.create(contract=contract,section=section,method='grind',state='confirmed',notes='СЕКРЕТ-ЗАДАНИЯ',created_by=leader)
            defect=Defect.objects.create(contract=contract,section=section,kilometre='123.456',number='123');JobDefect.objects.create(job=job,defect=defect)
            CustomerAccess.objects.create(user=customer,contract=contract,project_object=area,created_by=leader)
            MaterialRequest.objects.create(contract=contract,project_object=area,name='ТЕСТ Муфта П1',quantity=2,unit='шт',created_by=leader)
            env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_FIXTURE':json.dumps({k:str(v.pk) for k,v in dict(crew=crew,equipment=equipment,worker=worker,contract=contract,area=area,doc=doc).items()})}
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/complete616-browser.cjs')],env=env,capture_output=True,text=True,timeout=120)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(Admission.objects.count(),1);self.assertEqual(EquipmentCustody.objects.count(),1)
