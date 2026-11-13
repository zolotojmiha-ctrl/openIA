import os,secrets,subprocess
from urllib.parse import urlsplit
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from projects import test_stock as fixture,test_release624 as sample
from projects.models import LegalEntity,COMPANY_TAX_ID
from .rental_reminders import payload

class RentalReminderBrowserTests(StaticLiveServerTestCase):
    crew=fixture.StockTests.crew;rental=sample.Release624Tests.rental
    def test_exact_day_reminder_and_submission(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self));hire=self.rental()
        password=secrets.token_urlsafe(20);self.worker.set_password(password);self.worker.save()
        url=payload(hire,self.today)['attachments'][0]['payload']['buttons'][0][0]['url']
        parsed=urlsplit(url);path=parsed.path+'?'+parsed.query+'#'+parsed.fragment
        env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_PASSWORD':password,
            'ARMADA_TEST_PHONE':self.worker.phone,'ARMADA_REMINDER_PATH':path,'ARMADA_TEST_DATE':str(self.today)}
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/rental-reminder-browser.cjs')],env=env,capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        day=hire.days.get();self.assertEqual(day.date,self.today);self.assertEqual(day.kind,'work')
