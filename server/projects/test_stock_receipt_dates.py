"""Warehouse deliveries are independent of a crew's work schedule."""
import io
import json
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from . import stock, stock_planning as planning, stock_exchange as exchange
from .models import StockItem, StockMovement
from . import test_stock as base, test_stock621 as grid_tests


class StockReceiptDateTests(TestCase):
    setUpTestData = classmethod(base.StockTests.setUpTestData.__func__)
    wh = base.StockTests.wh
    data = base.StockTests.data
    item = base.StockTests.item
    balance = base.StockTests.balance
    crew = base.StockTests.crew
    row = grid_tests.Stock621Tests.row
    group = grid_tests.Stock621Tests.group
    payload = grid_tests.Stock621Tests.payload

    def holder(self, **dates):
        crew = self.crew()
        for key, value in dates.items():
            setattr(crew, key, value)
        crew.save()
        return self.wh('Володя Барабинск', brigade_assignment=crew)

    def test_inline_receipt_before_crew_assignment_and_retry(self):
        wh = self.holder()
        item = self.item(name='-', kind='coupling', unit='pcs', method='p1',
                         diameter_mm=720, length_mm=1500, wall_mm=10)
        date = self.today - timedelta(days=72)
        payload = self.payload([self.row(self.group(None), warehouse=wh.pk,
            quantity=1, name='Муфта П1 Ø720 × 1500 мм, стенка 10 мм',
            receipt_date=date, receipt_origin='Сокур')])
        self.client.force_login(self.leader)
        for _ in range(2):
            response = self.client.post(reverse('stock_inventory'),
                                        {'payload': json.dumps(payload)})
            self.assertEqual(response.status_code, 200, response.content)
            self.assertTrue(response.json()['ok'])
        item.refresh_from_db()
        self.assertEqual(self.balance(wh, item), 1)
        self.assertEqual(item.receipt_date, date)
        self.assertEqual(item.receipt_origin, 'Сокур')
        self.assertIsNone(item.reserved_job_id)
        self.assertEqual(StockMovement.objects.filter(item=item, kind='receipt').count(), 1)

    def test_delivery_after_crew_assignment_ends_for_couplings_and_materials(self):
        wh = self.holder(start_date=self.today-timedelta(days=20),
                         end_date=self.today-timedelta(days=10))
        for data, quantity in [
            (self.data(kind='coupling', unit='pcs', method='p1',
                       diameter_mm=720, length_mm=1500, wall_mm=10), 2),
            (self.data(kind='sealant', unit='pcs'), 5),
        ]:
            token = uuid.uuid4()
            for _ in range(2):
                planning.receive(self.leader, data, wh, quantity, self.today,
                                 'Поставка на склад', token)
        self.assertEqual(sum(b.quantity for b in wh.balances.all()), 7)
        self.assertEqual(StockMovement.objects.filter(kind='receipt').count(), 3)
        self.assertFalse(StockItem.objects.exclude(reserved_job=None).exists())

    def test_generic_receipt_does_not_require_active_crew_but_issue_does(self):
        wh = self.holder(start_date=self.today+timedelta(days=1))
        item = self.item()
        stock.move(self.leader, item.pk, 'receipt', 2, None, wh.pk,
                   self.today, 'Прямое поступление', uuid.uuid4())
        self.assertEqual(self.balance(wh, item), 2)
        source = self.wh('Основной')
        stock.move(self.leader, item.pk, 'receipt', 3, None, source.pk,
                   self.today, 'Поступление', uuid.uuid4())
        with self.assertRaises(ValidationError):
            stock.move(self.leader, item.pk, 'issue', 1, source.pk, wh.pk,
                       self.today, 'Выдача бригаде', uuid.uuid4())
        self.assertEqual((self.balance(source, item), self.balance(wh, item)), (3, 2))

    def test_excel_upload_preview_and_apply_before_crew_assignment(self):
        wh = self.holder()
        date = self.today - timedelta(days=72)
        row = ['НОВАЯ-1', '', wh.name, 'Муфта П1', 'Муфта', 'шт.',
               'П1', '720', '1500', '10', '', '', '', '', '', '',
               '2', 'нет', str(date), 'Сокур', '', '', '']
        content = exchange.book([(name, headers, [row] if name == 'Остатки' else [])
                                 for name, headers in exchange.HEADERS.items()])
        # Read and upload a real XLSX, using the same preview/confirmation as the UI.
        self.assertEqual(exchange.parse(io.BytesIO(content))['Остатки'][0]['values'][18], str(date))
        self.client.force_login(self.leader)
        url = reverse('stock_inventory_import')
        response = self.client.post(url, {'file': SimpleUploadedFile('stock.xlsx', content)})
        self.assertEqual(response.status_code, 200, response.content)
        preview = response.context['preview']
        self.assertTrue(preview, response.context['form'].errors)
        self.assertFalse(StockMovement.objects.exists())
        for _ in range(2):
            response = self.client.post(url, {'apply': '1', 'preview': preview})
            self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(sum(b.quantity for b in wh.balances.all()), 2)
        self.assertTrue(all(i.receipt_date == date for i in StockItem.objects.all()))
        self.assertEqual(StockMovement.objects.filter(kind='receipt').count(), 2)

    def test_future_dates_and_archived_warehouses_still_rejected(self):
        wh = self.holder()
        data = self.data(kind='coupling', unit='pcs', method='p1',
                         diameter_mm=720, length_mm=1500, wall_mm=10)
        with self.assertRaises(ValidationError):
            planning.receive(self.leader, data, wh, 1,
                             self.today+timedelta(days=1), 'Поставка', uuid.uuid4())
        wh.archived = True
        wh.save()
        with self.assertRaises(ValidationError):
            planning.receive(self.leader, data, wh, 1, self.today, 'Поставка', uuid.uuid4())
        self.assertFalse(StockItem.objects.exists())
