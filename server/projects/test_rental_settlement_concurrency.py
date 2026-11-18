"""Real PostgreSQL transactions: two writers compete for one transfer."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from django.db import connection,connections,close_old_connections
from django.test import TransactionTestCase
from django.core.exceptions import ValidationError
from accounts.models import User,AccessGrant
from . import test_release624 as fixture, rentals, rental_settlements as ledger
from .models import LegalEntity, COMPANY_TAX_ID


class RentalAllocationConcurrencyTests(TransactionTestCase):
    crew=fixture.Release624Tests.crew
    rental=fixture.Release624Tests.rental

    def setUp(self):
        if connection.vendor!='postgresql':self.skipTest('Requires PostgreSQL row locks')
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.Release624Tests.setUpTestData.__func__(type(self))
        self.hire=self.rental()
        rentals.save_day(self.worker,self.hire.pk,self.today,'work')
        self.invoice=ledger.create_invoice(self.leader,self.hire.pk,self.today,self.today,self.today,'ТЕСТ',1000,uuid.uuid4())
        self.payment=rentals.payment(self.leader,self.hire.pk,self.today,Decimal('1000'),'ТЕСТ',uuid.uuid4())
        AccessGrant.objects.get_or_create(user=self.supply,permission='finance.write',defaults={'granted_by':self.leader})

    def test_two_writers_cannot_allocate_same_credit_twice(self):
        def attempt(user_id):
            close_old_connections()
            try:
                user=User.objects.get(pk=user_id)
                ledger.allocate(user,self.hire.pk,self.payment.pk,self.invoice.pk,self.today,Decimal('700'),'ТЕСТ',uuid.uuid4())
                return 'saved'
            except ValidationError:return 'rejected'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(attempt,[self.leader.pk,self.supply.pk]))
        self.assertCountEqual(results,['saved','rejected'])
        self.assertEqual(ledger.invoice_balance(self.invoice)['paid'],700)

    def test_two_writers_cannot_spend_same_deposit_twice(self):
        rentals.classify_payment(self.leader,self.hire.pk,self.payment.pk,Decimal('1000'),1,'ТЕСТ залог')
        def attempt(user_id):
            close_old_connections()
            try:
                user=User.objects.get(pk=user_id)
                rentals.use_deposit(user,self.hire.pk,self.payment.pk,self.today,'refund',Decimal('700'),'ТЕСТ',uuid.uuid4())
                return 'saved'
            except ValidationError:return 'rejected'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(attempt,[self.leader.pk,self.supply.pk]))
        self.assertCountEqual(results,['saved','rejected'])
        self.assertEqual(rentals.totals(self.hire)['deposit'],300)
