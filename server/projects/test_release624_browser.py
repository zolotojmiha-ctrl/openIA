import os,secrets,subprocess,tempfile
from django.conf import settings
from django.test import override_settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from . import test_stock as stock_tests, test_release624 as release_tests
from .models import LegalEntity,COMPANY_TAX_ID,CustomerPublication,ContractObject,CustomerDocumentLink

class Release624BrowserTests(StaticLiveServerTestCase):
    wh=stock_tests.StockTests.wh;data=stock_tests.StockTests.data;item=stock_tests.StockTests.item;move=stock_tests.StockTests.move;crew=stock_tests.StockTests.crew
    rental=release_tests.Release624Tests.rental;customer=release_tests.Release624Tests.customer
    def test_mobile_rental_customer_account_and_document_exchange(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        stock_tests.StockTests.setUpTestData.__func__(type(self))
        r=self.rental();customer=self.customer();password=secrets.token_urlsafe(24)
        for u in [self.leader,self.worker,customer]:u.set_password(password);u.save()
        CustomerPublication.objects.create(contract=self.contract,project_object=self.obj,created_by=self.leader,snapshot={'rows':[{'pipeline':'ТЕСТ','km':'1','number':'624','state':'Устранён','latitude':'55.123456','longitude':'73.123456'}]})
        env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password,ARMADA_RENTAL_PATH=reverse('rental_detail',args=[r.pk]),ARMADA_CUSTOMER_DOCS_PATH=reverse('customer_documents',args=[self.contract.pk,self.obj.pk]),ARMADA_EMPLOYEE_PATH=reverse('employee_new'),ARMADA_SCOPE_ID=str(ContractObject.objects.get(contract=self.contract,project_object=self.obj).pk))
        with tempfile.TemporaryDirectory() as files,override_settings(PRIVATE_DOCUMENT_ROOT=files):
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/release624-browser.cjs')],env=env,capture_output=True,text=True,timeout=180)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(r.days.count(),1);self.assertEqual(r.days.get().hours,4)
        self.assertEqual(r.days.get().revisions.count(),2)
        self.assertEqual(CustomerDocumentLink.objects.count(),1)
