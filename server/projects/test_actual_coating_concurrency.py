from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless
from django.db import connection, connections
from django.test import TransactionTestCase
from . import test_stock as fixture
from .models import LegalEntity, COMPANY_TAX_ID, StockItem, StockBalance
from repairs.plans import revise


@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL row locks')
class ReplacementConcurrencyTests(TransactionTestCase):
    wh=fixture.StockTests.wh
    data=fixture.StockTests.data
    item=fixture.StockTests.item
    move=fixture.StockTests.move
    make_job=fixture.StockTests.make_job

    def test_two_jobs_cannot_reserve_one_replacement_twice(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        self.section.diameter_mm=720;self.section.save()
        jobs=[self.make_job(),self.make_job()]
        wh=self.wh()
        item=self.item(kind='coupling',unit='pcs',method='p2',length_mm=1500,wall_mm=8,diameter_mm=720)
        self.move(item,dst=wh,q=1)
        gate=Barrier(2)
        def change(job):
            connections.close_all()
            try:
                gate.wait(timeout=10)
                return revise(self.leader,job.pk,job.version,dict(method='p2',length_m=Decimal('1.5'),wall_mm=Decimal(8),composition=''),'ДДК')[0].pk
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(change,job) for job in jobs]
            self.assertEqual({f.result(timeout=25) for f in futures},{j.pk for j in jobs})
        self.assertEqual(StockItem.objects.filter(reserved_job__in=jobs).count(),1)
        self.assertEqual(StockBalance.objects.get(item=item).quantity,1)
