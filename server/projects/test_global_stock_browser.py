import os,secrets,subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from . import test_stock as fixture,test_stock_oversize as sized
from .models import LegalEntity,COMPANY_TAX_ID,StockItem

class GlobalStockBrowserTests(StaticLiveServerTestCase):
    wh=fixture.StockTests.wh;data=fixture.StockTests.data;make_job=fixture.StockTests.make_job
    job=sized.StockOversizeTests.job;piece=sized.StockOversizeTests.piece
    def test_preview_and_approve_at_four_widths(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        self.section.diameter_mm=720;self.section.save();self.warehouse=self.wh()
        short=self.job(length='1.5');long=self.job(length='3')
        for job,km in ((short,'100'),(long,'102')):
            defect=job.defects.get();defect.kilometre=km;defect.save()
        for length in (1500,1500,1000,2000):self.piece(length=length,wall=8)
        password=secrets.token_urlsafe(20);self.leader.set_password(password);self.leader.save()
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/global-stock-browser.cjs')],env=os.environ|{
            'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,
            'ARMADA_COVERAGE_PATH':reverse('stock_coverage')},capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(StockItem.objects.filter(reserved_job__isnull=False).count(),3)
