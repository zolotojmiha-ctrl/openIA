import os,secrets,tempfile,subprocess
from pathlib import Path
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from accounts.models import User,AccessGrant
from documents.models import Person
from repairs.excel import workbook
from .models import LegalEntity,ProjectObject,Contract,COMPANY_TAX_ID
from .registry_excel import HEADERS,PRIVATE_HEADERS

class RegistryBrowserTests(StaticLiveServerTestCase):
    def test_create_objects_contracts_people_with_preview(self):
        company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ компания'})[0]
        password=secrets.token_urlsafe(24)
        leader=User.objects.create_user('+70000000721',password,full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        a=ProjectObject.objects.create(legal_entity=company,name='ТЕСТ место А');b=ProjectObject.objects.create(legal_entity=company,name='ТЕСТ место Б')
        with tempfile.TemporaryDirectory() as root:
            for kind,rows in [
                ('objects',[['','ТЕСТ объект из Excel','Нефтепровод','Омск','В работе','2026-01-01','2026-12-31','']]),
                ('contracts',[['','ТЕСТ-КОНТРАКТ-EXCEL','24.10.2026','ТЕСТ работы по двум объектам','Устранение дефектов;Замена запорной арматуры','ТЕСТ заказчик','','В работе',str(a.pk)+';'+str(b.pk),'','1234,50','ТЕСТ примечание']]),
                ('people',[['','ТЕСТ Сотрудник из Excel','Инженер по ОТ и ПБ','0','']])]:
                Path(root,kind+'.xlsx').write_bytes(workbook(rows,headers=HEADERS[kind]+(PRIVATE_HEADERS if kind=='contracts' else [])))
            env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password,ARMADA_REGISTRY_FILES=root)
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/registry-browser.cjs')],env=env,capture_output=True,text=True,timeout=120)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(ProjectObject.objects.filter(name='ТЕСТ объект из Excel').count(),1)
        contract=Contract.objects.get(number='ТЕСТ-КОНТРАКТ-EXCEL');self.assertEqual(contract.objects_linked.count(),2);self.assertEqual(contract.work_kinds,['repairs','valves'])
        self.assertEqual(Person.objects.filter(full_name='ТЕСТ Сотрудник из Excel',user__isnull=True).count(),1)
