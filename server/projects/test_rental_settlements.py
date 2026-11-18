from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from . import rentals
from . import test_release624 as fixtures


class RentalSettlementRangeTests(TestCase):
    setUpTestData = classmethod(fixtures.Release624Tests.setUpTestData.__func__)
    crew = fixtures.Release624Tests.crew
    rental = fixtures.Release624Tests.rental

    def test_range_excludes_both_earlier_and_later_days(self):
        rental = self.rental()
        for offset in range(4):
            rentals.save_day(self.worker, rental.pk, self.today-timedelta(days=offset), 'work')
        result = rentals.period_totals(rental, self.today-timedelta(days=2), self.today-timedelta(days=1))
        self.assertEqual((result['charged'], result['hours'], result['missing']), (Decimal('2000'), 20, 0))
        self.assertEqual(len(result['days']), 2)
        period = rentals.close_period(self.leader, rental.pk, result['start'], result['end'])
        self.assertEqual(period.snapshot['period_charged'], '2000.00')
        self.assertEqual(period.snapshot['period_hours'], 20)
        self.assertEqual(Decimal(period.snapshot['charged']), Decimal('3000'))

    def test_missing_and_unpriced_are_not_a_complete_zero_bill(self):
        rental = self.rental()
        rental.rates.all().delete()
        rentals.save_day(self.worker, rental.pk, self.today, 'work')
        result = rentals.period_totals(rental, self.today-timedelta(days=1), self.today)
        self.assertEqual((result['missing'], result['unpriced'], result['hours']), (1, 1, 10))
        with self.assertRaises(ValidationError):
            rentals.close_period(self.leader, rental.pk, result['start'], result['end'])

    def test_invalid_range_rejected_without_writes(self):
        rental = self.rental()
        for start, end in [(self.today, self.today-timedelta(days=1)), (self.today, self.today+timedelta(days=1))]:
            with self.assertRaises(ValidationError):
                rentals.period_totals(rental, start, end)
        self.assertEqual(rental.periods.count(), 0)


class RentalDepositTests(TestCase):
    setUpTestData = classmethod(fixtures.Release624Tests.setUpTestData.__func__)
    crew = fixtures.Release624Tests.crew
    rental = fixtures.Release624Tests.rental

    def pay(self, rental, amount='150000', deposit='50000'):
        import uuid
        from finance.models import FinancialScope
        if rental.financial_scope_id is None:
            scope=FinancialScope.objects.create(legal_entity=self.company,contract=self.contract,title='ТЕСТ аренда',created_by=self.leader)
            rentals.link_finance(self.leader,rental.pk,scope)
            rental.refresh_from_db()
        return rentals.payment(self.leader, rental.pk, self.today, Decimal(amount), 'ТЕСТ перевод', uuid.uuid4(), Decimal(deposit))

    def test_split_keeps_single_cash_payment_and_cost_separate(self):
        from finance.services import summary
        rental = self.rental()
        rentals.save_day(self.worker, rental.pk, self.today, 'work')
        self.pay(rental)
        result = rentals.totals(rental)
        self.assertEqual((result['paid'], result['deposit'], result['cash_paid'], result['charged']), (100000,50000,150000,1000))
        self.assertEqual(rental.payments.count(), 1)
        self.assertEqual(summary(rental.financial_scope)['cash_out'], Decimal('150000'))

    def test_legacy_reclassification_keeps_cash_and_rejects_stale_update(self):
        import uuid
        rental = self.rental()
        row = self.pay(rental, deposit='0')
        row.purpose_confirmed = False
        row.save(update_fields=['purpose_confirmed'])
        self.assertEqual(rentals.totals(rental)['unconfirmed_payments'], 1)
        rentals.classify_payment(self.leader,rental.pk,row.pk,Decimal('50000'),1,'ТЕСТ назначение уточнено')
        self.assertEqual((rentals.totals(rental)['paid'],rentals.totals(rental)['deposit']), (100000,50000))
        self.assertEqual(rental.payments.count(),1)
        with self.assertRaises(ValidationError):
            rentals.classify_payment(self.leader,rental.pk,row.pk,0,1,'ТЕСТ устарело')
        with self.assertRaises(ValidationError):
            rentals.payment(self.leader,rental.pk,self.today,row.amount,row.note,row.token,0)

    def test_refund_offset_replay_and_reversal(self):
        import uuid
        from finance.services import summary
        rental = self.rental();row = self.pay(rental)
        token = uuid.uuid4()
        for _ in range(2):
            refund = rentals.use_deposit(self.leader,rental.pk,row.pk,self.today,'refund',Decimal('10000'),'ТЕСТ возврат',token)
        offset = rentals.use_deposit(self.leader,rental.pk,row.pk,self.today,'offset',Decimal('20000'),'ТЕСТ зачёт',uuid.uuid4())
        result = rentals.totals(rental)
        self.assertEqual((result['paid'],result['deposit'],result['cash_net']), (120000,20000,140000))
        self.assertEqual(row.deposit_uses.count(),2)
        self.assertEqual(summary(rental.financial_scope)['cash_out'],Decimal('140000'))
        with self.assertRaises(ValidationError):
            rentals.reverse_payment(self.leader,rental.pk,row.pk,'ТЕСТ отмена')
        with self.assertRaises(ValidationError):
            rentals.use_deposit(self.leader,rental.pk,row.pk,self.today,'refund',Decimal('20001'),'ТЕСТ сверх остатка',uuid.uuid4())
        with self.assertRaises(ValidationError):
            rentals.classify_payment(self.leader,rental.pk,row.pk,Decimal('29999'),1,'ТЕСТ ниже использованного')
        for operation in (refund,offset):
            rentals.reverse_deposit_use(self.leader,rental.pk,operation.pk,'ТЕСТ ошибочная операция')
        rentals.reverse_payment(self.leader,rental.pk,row.pk,'ТЕСТ отмена')
        result=rentals.totals(rental)
        self.assertEqual((result['paid'],result['deposit'],result['cash_net']), (0,0,0))

    def test_invalid_amounts_and_foreman_cannot_touch_finances(self):
        import uuid
        from django.core.exceptions import PermissionDenied
        rental=self.rental()
        for amount in ('NaN','Infinity','1.001','10000000000000000'):
            with self.assertRaises(ValidationError):
                self.pay(rental,amount=amount,deposit='0')
        with self.assertRaises(ValidationError):self.pay(rental,amount='100',deposit='101')
        row=self.pay(rental)
        with self.assertRaises(PermissionDenied):
            rentals.classify_payment(self.worker,rental.pk,row.pk,0,1,'ТЕСТ')
        with self.assertRaises(PermissionDenied):
            rentals.use_deposit(self.worker,rental.pk,row.pk,self.today,'offset',1,'ТЕСТ',uuid.uuid4())


class RentalInvoiceTests(TestCase):
    setUpTestData = classmethod(fixtures.Release624Tests.setUpTestData.__func__)
    crew = fixtures.Release624Tests.crew
    rental = fixtures.Release624Tests.rental

    def invoice(self,rental,start,end,price):
        import uuid
        from . import rental_settlements as ledger
        return ledger.create_invoice(self.leader,rental.pk,start,end,self.today,'ТЕСТ акт',Decimal(price),uuid.uuid4())

    def test_advance_and_later_payment_settle_selected_dates_only(self):
        import uuid
        from . import rental_settlements as ledger
        rental=self.rental()
        for offset in range(4):rentals.save_day(self.worker,rental.pk,self.today-timedelta(days=offset),'work')
        bill=self.invoice(rental,rental.placement.start_date,self.today-timedelta(days=1),'3000')
        first=rentals.payment(self.leader,rental.pk,self.today-timedelta(days=3),Decimal('1000'),'ТЕСТ аванс',uuid.uuid4(),Decimal('500'))
        second=rentals.payment(self.leader,rental.pk,self.today,Decimal('2500'),'ТЕСТ доплата',uuid.uuid4())
        for payment,value in [(first,'500'),(second,'2500')]:
            token=uuid.uuid4()
            for _ in range(2):ledger.allocate(self.leader,rental.pk,payment.pk,bill.pk,self.today,Decimal(value),'ТЕСТ зачёт',token)
        result=ledger.summary(rental)
        self.assertEqual((result['next_start'],result['new_charged'],result['new_hours'],result['invoice_debt'],result['unallocated']), (self.today,1000,10,0,0))
        self.assertEqual((rental.payments.count(),bill.allocations.count()),(2,2))
        self.assertEqual((rentals.totals(rental)['charged'],rentals.totals(rental)['deposit']),(4000,500))
        with self.assertRaises(ValidationError):rentals.reverse_payment(self.leader,rental.pk,second.pk,'ТЕСТ')
        allocation=second.allocations.get()
        ledger.reverse_allocation(self.leader,rental.pk,allocation.pk,'ТЕСТ отмена')
        result=ledger.summary(rental)
        self.assertEqual((result['next_start'],result['invoice_debt']), (rental.placement.start_date,2500))

    def test_later_paid_period_does_not_hide_gap_and_disagreement_cannot_settle(self):
        import uuid
        from . import rental_settlements as ledger
        rental=self.rental()
        for offset in range(4):rentals.save_day(self.worker,rental.pk,self.today-timedelta(days=offset),'work')
        bill=self.invoice(rental,self.today,self.today,'1000')
        payment=rentals.payment(self.leader,rental.pk,self.today,Decimal('3000'),'ТЕСТ',uuid.uuid4())
        ledger.allocate(self.leader,rental.pk,payment.pk,bill.pk,self.today,1000,'ТЕСТ',uuid.uuid4())
        self.assertEqual(ledger.summary(rental)['next_start'],rental.placement.start_date)
        dispute=self.invoice(rental,rental.placement.start_date,self.today-timedelta(days=1),'2999')
        with self.assertRaises(ValidationError):ledger.allocate(self.leader,rental.pk,payment.pk,dispute.pk,self.today,1000,'ТЕСТ',uuid.uuid4())
        with self.assertRaises(ValidationError):rentals.reopen_period(self.leader,rental.pk,dispute.period_id,'ТЕСТ')
        original=dispute.period.snapshot
        ledger.void_invoice(self.leader,rental.pk,dispute.pk,'ТЕСТ новый документ')
        dispute.period.refresh_from_db();self.assertEqual(dispute.period.snapshot,original)

    def test_allocation_cannot_exceed_credit_or_consume_deposit(self):
        import uuid
        from . import rental_settlements as ledger
        rental=self.rental();rentals.save_day(self.worker,rental.pk,self.today,'work')
        bill=self.invoice(rental,self.today,self.today,'1000')
        payment=rentals.payment(self.leader,rental.pk,self.today,Decimal('1000'),'ТЕСТ',uuid.uuid4(),Decimal('600'))
        with self.assertRaises(ValidationError):ledger.allocate(self.leader,rental.pk,payment.pk,bill.pk,self.today,401,'ТЕСТ',uuid.uuid4())
        ledger.allocate(self.leader,rental.pk,payment.pk,bill.pk,self.today,400,'ТЕСТ',uuid.uuid4())
        with self.assertRaises(ValidationError):rentals.classify_payment(self.leader,rental.pk,payment.pk,Decimal('601'),1,'ТЕСТ')
        use=rentals.use_deposit(self.leader,rental.pk,payment.pk,self.today,'offset',Decimal('600'),'ТЕСТ согласовано',uuid.uuid4())
        ledger.allocate(self.leader,rental.pk,payment.pk,bill.pk,self.today,600,'ТЕСТ',uuid.uuid4())
        with self.assertRaises(ValidationError):rentals.reverse_deposit_use(self.leader,rental.pk,use.pk,'ТЕСТ')

    def test_exports_use_frozen_snapshot_and_recheck_financial_rights(self):
        from django.urls import reverse
        import io,zipfile
        rental=self.rental();rentals.save_day(self.worker,rental.pk,self.today,'work')
        bill=self.invoice(rental,self.today,self.today,'1000')
        self.client.force_login(self.leader)
        url=reverse('rental_invoice_export',args=[rental.pk,bill.pk])
        response=self.client.get(url)
        self.assertEqual(response.status_code,200)
        with zipfile.ZipFile(io.BytesIO(response.content)) as book:
            sheet=book.read('xl/worksheets/sheet1.xml').decode()
            self.assertIn('t="n"',sheet)
            self.assertIn('1000',sheet)
        print_url=reverse('rental_invoice_print',args=[rental.pk,bill.pk])
        self.assertContains(self.client.get(print_url),'Тариф')
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(url).status_code,403)
        self.assertEqual(self.client.get(print_url).status_code,403)

    def test_equipment_cost_spans_placements_without_moving_advance(self):
        import uuid
        from .models import EquipmentAssignment
        from django.core.exceptions import PermissionDenied
        rental=self.rental()
        rentals.save_day(self.leader,rental.pk,rental.placement.start_date,'work')
        rentals.payment(self.leader,rental.pk,self.today,Decimal('3000'),'ТЕСТ прежний аванс',uuid.uuid4())
        rental.placement.end_date=self.today-timedelta(days=1);rental.placement.save(update_fields=['end_date'])
        placement=EquipmentAssignment.objects.create(equipment=rental.placement.equipment,project_object=self.obj,contract=self.contract,start_date=self.today,created_by=self.leader)
        other=rentals.configure(self.leader,placement.pk,'ТЕСТ другой договор','ТЕСТ другой договор')
        rentals.add_rate(self.leader,other.pk,self.today,'hour',Decimal('200'),Decimal('80'))
        rentals.save_day(self.leader,other.pk,self.today,'work')
        self.assertEqual(rentals.equipment_cost(self.leader,placement.equipment_id)['charged'],3000)
        self.assertEqual(rentals.totals(other)['debt'],2000)
        self.assertEqual(rentals.totals(rental)['advance'],2000)
        with self.assertRaises(PermissionDenied):rentals.equipment_cost(self.worker,placement.equipment_id)
