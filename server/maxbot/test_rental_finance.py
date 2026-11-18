import uuid
from datetime import timedelta
from decimal import Decimal
from django.test import TestCase,override_settings
from projects import test_release624 as fixture, rentals, rental_settlements as ledger
from . import tests as config, worker
from .models import Binding,Inbox,Outbox


@override_settings(**config.CONFIG)
class RentalFinanceBotTests(TestCase):
    setUpTestData=classmethod(fixture.Release624Tests.setUpTestData.__func__)
    crew=fixture.Release624Tests.crew
    rental=fixture.Release624Tests.rental

    def setUp(self):
        self.hire=self.rental()
        self.binding=Binding.objects.create(user=self.leader,max_user=101,chat=201,session_version=self.leader.session_version)

    def send(self,text):
        event=Inbox.objects.create(key=uuid.uuid4().hex,max_user=101,chat=201,payload={'kind':'message_created','text':text,'photos':[]})
        worker.process(event.pk);event.refresh_from_db()
        self.assertEqual(event.state,'done')
        self.binding.refresh_from_db()
        return Outbox.objects.get(key__startswith=f'in:{event.pk}:')

    def fill(self,action,values):
        self.send(f'rentfin:start:{self.hire.pk}:{action}')
        for value in values:self.send(str(value))
        self.assertEqual(self.binding.menu_scope['rental_finance']['state'],'confirm')

    def test_payment_preview_confirmation_replay_and_cancel(self):
        self.fill('payment',[self.today,'150000','ТЕСТ перевод','50000'])
        self.assertEqual(self.hire.payments.count(),0)
        self.send('rentfin:confirm');self.send('rentfin:confirm')
        payment=self.hire.payments.get()
        self.assertEqual((payment.amount,payment.deposit_amount),(150000,50000))
        self.fill('deposit',[payment.pk,self.today,'offset','10000','ТЕСТ согласовано'])
        self.send('rentfin:cancel')
        self.assertEqual(payment.deposit_uses.count(),0)
        self.fill('deposit',[payment.pk,self.today,'refund','10000','ТЕСТ возврат'])
        self.send('rentfin:confirm')
        self.assertEqual(rentals.totals(self.hire)['deposit'],40000)

    def test_invoice_and_allocation_use_same_ledger(self):
        rentals.save_day(self.worker,self.hire.pk,self.today,'work')
        self.fill('invoice',[self.today,self.today,self.today,'ТЕСТ счёт','1000','-'])
        self.send('rentfin:confirm')
        invoice=ledger.RentalInvoice.objects.get()
        self.fill('payment',[self.today,'1000','ТЕСТ оплата','0']);self.send('rentfin:confirm')
        payment=self.hire.payments.get()
        self.fill('allocate',[payment.pk,invoice.pk,self.today,'1000','ТЕСТ зачёт']);self.send('rentfin:confirm')
        self.assertEqual(ledger.invoice_balance(invoice)['debt'],0)
        page=self.send(f'rentfin:home:{self.hire.pk}:0')
        self.assertIn('Сверка №',page.body['text'])
        self.assertIn('Привязка №',page.body['text'])

    def test_stale_preview_foreman_and_revoked_access_do_not_write(self):
        self.fill('payment',[self.today,'1000','ТЕСТ','0'])
        rentals.save_day(self.worker,self.hire.pk,self.today,'work')
        page=self.send('rentfin:confirm')
        self.assertEqual(self.hire.payments.count(),0)
        self.assertIn('изменились',page.body['text'])
        self.binding.user=self.worker;self.binding.session_version=self.worker.session_version;self.binding.save()
        self.send(f'rentfin:start:{self.hire.pk}:payment')
        self.assertEqual(self.hire.payments.count(),0)

    def test_read_only_range_does_not_create_invoice_or_payment(self):
        rentals.save_day(self.worker,self.hire.pk,self.today,'work')
        self.send(f'rentfin:start:{self.hire.pk}:range')
        self.send(str(self.today));page=self.send(str(self.today))
        self.assertIn('10 ч',page.body['text'])
        self.assertEqual(self.hire.periods.count(),0)
        self.assertEqual(self.hire.payments.count(),0)

    def test_picker_has_record_buttons_and_pagination(self):
        for _ in range(6):rentals.payment(self.leader,self.hire.pk,self.today,Decimal('1'),'ТЕСТ',uuid.uuid4())
        page=self.send(f'rentfin:start:{self.hire.pk}:allocate')
        self.assertIn('Следующие записи',str(page.body))
        self.assertIn('rentfin:value:',str(page.body))
        next_page=self.send('rentfin:choices:1')
        self.assertIn('Предыдущие записи',str(next_page.body))
