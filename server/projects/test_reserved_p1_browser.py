import os,secrets,subprocess
from decimal import Decimal
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from . import test_stock as fixture,test_reserved_p1_demand as sample,test_stock_oversize as sized
from .models import LegalEntity,COMPANY_TAX_ID
from repairs.models import Defect,JobDefect


class ReservedP1BrowserTests(StaticLiveServerTestCase):
    wh=fixture.StockTests.wh;data=fixture.StockTests.data;make_job=fixture.StockTests.make_job
    job=sized.StockOversizeTests.job;piece=sized.StockOversizeTests.piece
    common_job=sample.ReservedP1DemandTests.common_job;reserve=sample.ReservedP1DemandTests.reserve
    mailing=sample.ReservedP1DemandTests.mailing
    def test_only_uncovered_remainder_appears_with_one_km_on_phone_and_customer_preview(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        self.section.diameter_mm=720;self.section.save();self.warehouse=self.wh()
        covered=self.common_job();self.reserve(covered,1500);self.reserve(covered,1000)
        partial=self.job(length='2.5',composition='1.5+1')
        first=partial.defects.get();first.kilometre=Decimal('200.111');first.number='TEST-REMAINDER-A';first.pipe_section='TEST-PIPE';first.save()
        second=Defect.objects.create(contract=self.contract,section=self.section,kilometre=Decimal('200.111'),number='TEST-REMAINDER-B',pipe_section='TEST-PIPE')
        JobDefect.objects.create(job=partial,defect=second);self.reserve(partial,1500)
        config=self.mailing();password=secrets.token_urlsafe(20);self.leader.set_password(password);self.leader.save()
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/reserved-p1-demand-browser.cjs')],env=os.environ|{
            'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,
            'ARMADA_COVERAGE_PATH':reverse('stock_coverage'),'ARMADA_PREVIEW_PATH':reverse('demand_mail_preview',args=[config.pk])},capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
