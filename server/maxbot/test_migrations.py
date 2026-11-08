from django.core.management import call_command
from django.test import TransactionTestCase
from projects.models import LegalEntity, COMPANY_TAX_ID
from projects import test_stock as fixture
from repairs.models import Job, Report
from django.utils import timezone
import io


class MaxMigrationTests(TransactionTestCase):
    def test_additive_max_migrations_preserve_existing_users_and_reports(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID, defaults={'name': 'ТЕСТ'})
        fixture.StockTests.setUpTestData.__func__(type(self))
        job = Job.objects.create(contract=self.contract, section=self.section, method='grind', created_by=self.leader, assigned_to=self.worker)
        report = Report.objects.create(job=job, author=self.worker, kind='complete', outcome='complete', performed_date=timezone.localdate())
        output = io.StringIO()
        try:
            call_command('migrate', 'maxbot', 'zero', verbosity=0, stdout=output)
            self.assertTrue(Report.objects.filter(pk=report.pk, author=self.worker).exists())
        finally:
            call_command('migrate', 'maxbot', verbosity=0, stdout=output)
        self.assertTrue(Report.objects.filter(pk=report.pk, job=job).exists())
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.role, 'foreman')
        from .models import Binding
        self.assertEqual(Binding.objects.count(), 0)
