import os,secrets,subprocess
from decimal import Decimal
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from . import test_stock as base,test_release624 as rental_tests
from .models import LegalEntity,COMPANY_TAX_ID


class RentalMoneyBrowserTests(StaticLiveServerTestCase):
    crew=base.StockTests.crew
    rental=rental_tests.Release624Tests.rental

    def test_tariff_and_advance_save_from_equipment_card(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        base.StockTests.setUpTestData.__func__(type(self))
        rental=self.rental();rental.rates.all().delete()
        password=secrets.token_urlsafe(20)
        self.leader.set_password(password);self.leader.save()
        env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,
            'ARMADA_TEST_PHONE':self.leader.phone,'ARMADA_EQUIPMENT_PATH':reverse('equipment_detail',args=[rental.placement.equipment_id]),
            'ARMADA_RATE_START':str(rental.placement.start_date),'ARMADA_PAYMENT_DATE':str(self.today)}
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/rental-money-browser.cjs')],
            env=env,capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        rate=rental.rates.get()
        self.assertEqual((rate.work_price,rate.idle_price),(Decimal('21000.00'),Decimal('8400.00')))
        self.assertEqual(rental.payments.get().amount,Decimal('15500.00'))
