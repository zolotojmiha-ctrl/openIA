import io
import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from repairs.services import report_work
from . import test_stock as base
from . import stock_planning as planning, stock_groups as groups
from . import stock_assignment as assignment, stock_exchange as exchange
from .models import StockItem


class StockOversizeTests(TestCase):
    setUpTestData = classmethod(base.StockTests.setUpTestData.__func__)
    wh = base.StockTests.wh
    data = base.StockTests.data
    balance = base.StockTests.balance
    make_job = base.StockTests.make_job

    def setUp(self):
        self.section.diameter_mm = 720
        self.section.save()
        self.warehouse = self.wh()
        self.client.force_login(self.leader)

    def job(self, method='p1', length='1', wall=8, composition=''):
        job = self.make_job()
        job.method, job.length_m, job.wall_mm, job.composition = method, Decimal(length), wall, composition
        job.save()
        return job

    def piece(self, method='p1', length=1500, wall=10, diameter=720):
        previous = set(StockItem.objects.values_list('pk', flat=True))
        planning.receive(self.leader, self.data(kind='coupling', unit='pcs',
            method=method, length_mm=length, wall_mm=wall, diameter_mm=diameter),
            self.warehouse, 1, self.today, 'Тестовая поставка', uuid.uuid4())
        return StockItem.objects.exclude(pk__in=previous).get()

    def rows(self):
        return self.client.get(reverse('stock_coverage')).context['rows']

    def result(self, job, pieces, outcome='complete', action='result'):
        job.refresh_from_db()
        return report_work(self.leader, job.pk, action, {
            'token': uuid.uuid4(), 'expected_version': job.version,
            'performed_date': self.today, 'outcome': outcome, 'method': job.method,
            'length_m': job.length_m, 'composition': job.composition,
            'note': 'Проверка монтажа', 'problem': '', 'latitude': None,
            'longitude': None, 'couplings': pieces, 'files': [],
        })

    def test_p1_and_p2_oversize_visible_reservable_installable_and_reversible(self):
        for method in ('p1', 'p2'):
            with self.subTest(method=method):
                job = self.job(method)
                item = self.piece(method)
                group = next(g for g in groups.groups(self.leader) if g['item'].pk == item.pk)
                row = next(r for r in assignment.job_rows(self.leader, group) if r['job'].pk == job.pk)
                self.assertTrue(row['compatible'], row['reason'])
                self.assertEqual(row['maximum'], 1)
                item = planning.reserve(self.leader, item.pk, item.version, job.pk)
                self.assertEqual(self.balance(self.warehouse, item), 1)
                extra = self.piece(method, 2000, 12)
                with self.assertRaises(ValidationError):
                    planning.reserve(self.leader, extra.pk, extra.version, job.pk)
                report = self.result(job, [item.coupling])
                self.assertEqual(self.balance(self.warehouse, item), 0)
                self.assertEqual(report.coupling_snapshot[0]['length_mm'], 1500)
                self.result(job, [], action='correction')
                self.assertEqual(self.balance(self.warehouse, item), 1)
                item.refresh_from_db()
                self.assertEqual((item.length_mm, item.wall_mm), (1500, 10))

    def test_shorter_thinner_wrong_type_and_wrong_diameter_rejected_by_ui_and_backend(self):
        for method in ('p1', 'p2'):
            job = self.job(method)
            variants = [dict(length=500), dict(wall=6),
                        dict(method='p2' if method == 'p1' else 'p1'), dict(diameter=530)]
            for variant in variants:
                with self.subTest(method=method, variant=variant):
                    item = self.piece(**({'method': method} | variant))
                    group = next(g for g in groups.groups(self.leader)
                                 if any(m['item'].pk == item.pk for m in g['members']))
                    row = next(r for r in assignment.job_rows(self.leader, group) if r['job'].pk == job.pk)
                    self.assertFalse(row['compatible'])
                    with self.assertRaises(ValidationError):
                        planning.reserve(self.leader, item.pk, item.version, job.pk)
                    item.refresh_from_db()
                    self.assertIsNone(item.reserved_job_id)

    def test_mixed_construction_counts_each_piece_once_after_partial_installation(self):
        job = self.job(length='2.5', composition='1+1.5')
        long = self.piece(length=2000)
        short = self.piece(length=1000, wall=8)
        long = planning.reserve(self.leader, long.pk, long.version, job.pk)
        short = planning.reserve(self.leader, short.pk, short.version, job.pk)
        extra = self.piece()
        with self.assertRaises(ValidationError):
            planning.reserve(self.leader, extra.pk, extra.version, job.pk)
        self.result(job, [long.coupling], outcome='partial')
        rows = self.rows()
        self.assertEqual(sum(r['need'] for r in rows), 1)
        self.assertEqual(sum(r['reserved'] for r in rows), 1)
        self.assertEqual(sum(r['missing'] for r in rows), 0)

    def test_shared_oversize_supply_not_double_counted_and_reservation_retained(self):
        first = self.job()
        self.job(length='1.5', wall=10)
        item = self.piece()
        rows = self.rows()
        self.assertEqual(sum(r['need'] for r in rows), 2)
        self.assertEqual(sum(r['free'] for r in rows), 1)
        self.assertEqual(sum(r['missing'] for r in rows), 1)
        planning.reserve(self.leader, item.pk, item.version, first.pk)
        rows = self.rows()
        self.assertEqual(sum(r['reserved'] for r in rows), 1)
        self.assertEqual(sum(r['free'] for r in rows), 0)
        self.assertEqual(sum(r['missing'] for r in rows), 1)

    def test_coverage_reassigns_shared_size_so_all_compatible_needs_are_met(self):
        self.job(length='1', wall=10)
        self.job(length='1.5', wall=8)
        self.piece(length=1500, wall=10)
        self.piece(length=1000, wall=10)
        rows = self.rows()
        self.assertEqual(sum(r['free'] for r in rows), 2)
        self.assertEqual(sum(r['missing'] for r in rows), 0)

    def test_excel_can_reserve_oversize_and_keeps_actual_dimensions(self):
        job = self.job('p2')
        item = self.piece('p2')
        response = self.client.get(reverse('stock_inventory_export'))
        tables = exchange.parse(io.BytesIO(response.content))
        group = groups.groups(self.leader)[0]
        tables['Остатки'][0]['values'][17] = 'да'
        tables['Распределение'] = [{'row': 2, 'values': [group['id'],
            planning.job_label(job), '1', self.warehouse.name]}]
        exchange.apply(self.leader, tables)
        item.refresh_from_db()
        self.assertEqual(item.reserved_job_id, job.pk)
        self.assertEqual((item.length_mm, item.wall_mm), (1500, 10))

    def test_p3_keeps_exact_dimensions(self):
        job = self.job('p3')
        item = self.piece('p3')
        with self.assertRaises(ValidationError):
            planning.reserve(self.leader, item.pk, item.version, job.pk)
        exact = self.piece('p3', length=1000, wall=8)
        planning.reserve(self.leader, exact.pk, exact.version, job.pk)

    def test_changing_requirement_after_reservation_blocks_undersized_installation(self):
        job = self.job()
        item = self.piece()
        item = planning.reserve(self.leader, item.pk, item.version, job.pk)
        job.wall_mm = 12
        job.save()
        with self.assertRaises(ValidationError):
            self.result(job, [item.coupling])
        self.assertEqual(self.balance(self.warehouse, item), 1)
        self.assertFalse(job.reports.exists())

    def test_reduced_composition_cannot_consume_two_pieces_for_one_component(self):
        job = self.job(length='2', composition='1+1')
        first, second = self.piece(), self.piece()
        first = planning.reserve(self.leader, first.pk, first.version, job.pk)
        second = planning.reserve(self.leader, second.pk, second.version, job.pk)
        job.length_m, job.composition = 1, '1'
        job.save()
        with self.assertRaises(ValidationError):
            self.result(job, [first.coupling, second.coupling])
        self.assertEqual(sum(b.quantity for b in self.warehouse.balances.all()), 2)
        self.assertFalse(job.reports.exists())
