import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from django.db import connection,connections
from django.test import TransactionTestCase
from django.utils import timezone
from . import test_stock as fixture,demand_mail
from .models import LegalEntity,COMPANY_TAX_ID,DemandMailing,DemandMailRun
from postoffice.models import Mailbox,Letter

@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL row locks')
class DemandMailConcurrency(TransactionTestCase):
    make_job=fixture.StockTests.make_job
    def test_two_workers_enqueue_one_week_once(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        self.section.diameter_mm=720;self.section.save();self.make_job()
        box=Mailbox.objects.create(address='noreply@example.org',state='active',created_by=self.leader)
        config=DemandMailing.objects.create(name='ТЕСТ',owner=self.leader,mailbox=box,recipients='customer@example.org',enabled=True)
        config.project_objects.add(self.obj)
        gate=Barrier(2)
        def enqueue():
            connections.close_all()
            try:
                gate.wait(timeout=10)
                return demand_mail.enqueue(self.leader,config.pk,uuid.uuid4(),automatic=True,week=timezone.localdate())
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(enqueue) for _ in range(2)]
            for f in futures:f.result(timeout=30)
        self.assertEqual(DemandMailRun.objects.count(),1)
        self.assertEqual(Letter.objects.count(),1)
