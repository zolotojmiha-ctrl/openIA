import os,secrets,subprocess
from datetime import timedelta
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from . import test_release624 as fixture,rentals


class RentalSettlementsBrowserTests(StaticLiveServerTestCase):
    crew=fixture.Release624Tests.crew
    rental=fixture.Release624Tests.rental

    def test_mobile_split_payment_and_invoice_settlement(self):
        fixture.Release624Tests.setUpTestData.__func__(type(self))
        rental=self.rental()
        for offset in range(4):rentals.save_day(self.worker,rental.pk,self.today-timedelta(days=offset),'work')
        password=secrets.token_urlsafe(20)
        self.leader.set_password(password);self.leader.save()
        env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,
            'ARMADA_TEST_PHONE':self.leader.phone,'ARMADA_EQUIPMENT_PATH':reverse('equipment_detail',args=[rental.placement.equipment_id]),
            'ARMADA_RATE_START':str(rental.placement.start_date),'ARMADA_PERIOD_END':str(self.today-timedelta(days=1)),'ARMADA_PAYMENT_DATE':str(self.today)}
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/rental-settlements-browser.cjs')],env=env,capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual((rental.payments.get().amount,rental.payments.get().deposit_amount),(150000,50000))
