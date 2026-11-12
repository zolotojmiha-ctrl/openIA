import importlib
import io
import zipfile
from decimal import Decimal
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from django.apps import apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.urls import reverse

from accounts.forms import EmployeeForm
from accounts.models import AccessGrant, User
from accounts.permissions import permitted
from repairs.importer import read_book
from repairs.models import Defect, JobDefect
from . import stock, test_stock as base, test_release624 as previous
from .models import ObjectAccess, ContractAccess, StockMovement
from .report_excel import workbook, NS


class StockReportTests(TestCase):
    setUpTestData = classmethod(base.StockTests.setUpTestData.__func__)
    wh = base.StockTests.wh
    data = base.StockTests.data
    item = base.StockTests.item
    move = base.StockTests.move
    make_job = base.StockTests.make_job
    profile = previous.Release624Tests.profile

    def setUp(self):
        self.section.diameter_mm = 720
        self.section.save()
        self.pto = User.objects.create_user('+70000000728', '123456', role='pto', full_name='ТЕСТ ПТО', must_change_password=False)
        ObjectAccess.objects.create(user=self.pto, project_object=self.obj, granted_by=self.leader)
        ContractAccess.objects.create(user=self.pto, contract=self.contract, granted_by=self.leader)
        AccessGrant.objects.create(user=self.pto, permission='repairs.prepare', granted_by=self.leader)

    def grant_stock(self):
        migration = importlib.import_module('accounts.migrations.0028_pto_stock_access')
        migration.grant_stock(apps, SimpleNamespace(connection=connection))

    def sheet(self, content, name):
        return read_book(SimpleUploadedFile('report.xlsx', content), sheet_name=name)['cells']

    def test_existing_pto_gets_full_stock_access_without_financial_rights(self):
        self.client.force_login(self.pto)
        self.assertEqual(self.client.get(reverse('coating_profiles')).status_code, 403)
        before = list(self.pto.grants.values_list('pk', flat=True))
        self.grant_stock()
        grant = self.pto.grants.get(permission='stock.manage')
        self.grant_stock()
        self.assertEqual(self.pto.grants.get(permission='stock.manage').pk, grant.pk)
        self.assertTrue(self.pto.grants.filter(pk__in=before).exists())
        self.assertFalse(permitted(self.pto, 'finance.read'))
        self.assertFalse(permitted(self.pto, 'finance.write'))
        self.assertFalse(self.worker.grants.filter(permission='stock.manage').exists())
        for name in ['stock_inventory', 'stock_warehouses', 'coating_profiles', 'stock_coverage', 'repair_materials']:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertContains(self.client.get(reverse('coating_profiles')), 'Добавить профиль')
        profile, warehouse, items = self.profile()
        payload = {'expected_version': profile.version, 'method': 'p2', 'diameter_mm': 720,
                   'length_mm': 1000, 'full_length_mm': 1460, 'area_per_m': '2.55', 'extra_length_mm': 1000,
                   'basis': 'ТЕСТ Уточнённая карта', 'active': 'on', **{k: str(v.pk) for k, v in items.items()}}
        response = self.client.post(reverse('coating_profile_edit', args=[profile.pk]), payload)
        self.assertEqual(response.status_code, 302, response.context and response.context['form'].errors)
        profile.refresh_from_db()
        self.assertEqual(profile.basis, 'ТЕСТ Уточнённая карта')
        self.assertEqual(profile.created_by_id, self.leader.pk)
        self.pto.is_active = False
        self.assertFalse(permitted(self.pto, 'stock.manage'))

    def test_pto_preset_covers_stock_but_not_finance(self):
        form = EmployeeForm({'full_name': 'ТЕСТ Новый ПТО', 'phone': '+70000000729', 'role': 'pto', 'profile': 'pto', 'is_active': 'on'}, actor=self.leader)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn('stock.manage', form.cleaned_data['permissions'])
        self.assertFalse({'finance.read', 'finance.write'} & set(form.cleaned_data['permissions']))

    def test_demand_export_matches_summary_and_keeps_expenses_separate(self):
        self.grant_stock()
        profile, warehouse, items = self.profile()
        job = self.make_job()
        defect = Defect.objects.create(contract=self.contract, section=self.section, kilometre=2, number='ТЕСТ ВТОРОЙ')
        JobDefect.objects.create(job=job, defect=defect)
        self.move(items['primer'], 'consume', Decimal('1.25'), src=warehouse, job=job)
        self.client.force_login(self.pto)
        response = self.client.get(reverse('repair_materials'))
        self.assertContains(response, 'Показать по км')
        expected = response.context['grouped']
        movements = StockMovement.objects.count()
        download = self.client.get(reverse('repair_materials_export'))
        self.assertEqual(download.status_code, 200)
        cells = self.sheet(download.content, 'Потребность')
        for row, data in enumerate(expected, 2):
            self.assertEqual(cells[(row, 1)], data['name'])
            self.assertEqual(Decimal(cells[(row, 3)]), data['missing'])
        coupling = next(data for data in expected if data['unit'] == 'шт.')
        self.assertEqual(coupling['need'], 1)  # two defects, one construction
        expense = self.sheet(download.content, 'Расход')
        self.assertEqual(Decimal(expense[(2, 9)]), Decimal('1.25'))
        self.assertEqual(StockMovement.objects.count(), movements)
        self.assertEqual({p.strip() for p in download['Cache-Control'].split(',')}, {'private', 'no-store'})

    def test_coupling_export_uses_displayed_filter_counts_and_separate_unknowns(self):
        self.grant_stock()
        job = self.make_job()
        unknown = self.make_job()
        unknown.wall_mm = None
        unknown.save()
        warehouse = self.wh(project_object=self.obj)
        item = self.item(kind='coupling', unit='pcs', method='p2', length_mm=1000, wall_mm=8, diameter_mm=720)
        self.move(item, dst=warehouse, q=1)
        self.client.force_login(self.pto)
        url = reverse('stock_coverage')
        params = {'contract': str(self.contract.pk), 'object': str(self.obj.pk)}
        page = self.client.get(url, params)
        self.assertContains(page, 'Ведомость в Excel')
        self.assertContains(page, 'Расчёт неполный: требуют уточнения')
        self.assertContains(page, 'все доступные работы')
        self.assertNotContains(page,'Подобрано из свободных</th>')
        self.assertIn('contract=', page.context['export_query'])
        self.assertIn('object=', page.context['export_query'])
        response = self.client.get(url, dict(params, export='xlsx'))
        self.assertEqual(response.status_code, 200)
        cells = self.sheet(response.content, 'Муфты')
        self.assertEqual(page.context['rows'],[])
        self.assertNotIn((2,1),cells)
        self.assertEqual(len(page.context['unknown']), 1)
        needs = self.client.get(reverse('repair_materials'))
        self.assertContains(needs, 'Расчёт неполный: нужно уточнить данные')
        self.assertNotContains(needs,'Работы с нехваткой')
        self.assertTrue(self.sheet(response.content, 'Нужно уточнить')[(2, 2)])
        self.assertEqual(self.client.get(url, dict(params, contract='invalid', export='xlsx')).status_code, 404)
        other_filter = self.client.get(url, {'contract': str(self.contract.pk), 'object': str(job.pk), 'export': 'xlsx'})
        self.assertNotIn((2, 1), self.sheet(other_filter.content, 'Муфты'))

    def test_exports_require_internal_report_access(self):
        customer = previous.Release624Tests.customer(self)
        for user in [self.worker, customer]:
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('repair_materials_export')).status_code, 403)
            self.assertEqual(self.client.get(reverse('stock_coverage'), {'export': 'xlsx'}).status_code, 403)

    def test_report_quantities_are_excel_numbers_and_text_is_not_a_formula(self):
        content = workbook([('Потребность', ['Материал', 'Количество'], [['=1+1', Decimal('1.25')]])])
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            root = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        self.assertEqual(root.find(f'.//{{{NS}}}c[@r="A2"]').get('t'), 'inlineStr')
        amount = root.find(f'.//{{{NS}}}c[@r="B2"]')
        self.assertIsNone(amount.get('t'))
        self.assertEqual(amount.find(f'{{{NS}}}v').text, '1.25')
        self.assertEqual(self.sheet(content, 'Потребность')[(2, 1)], '=1+1')
