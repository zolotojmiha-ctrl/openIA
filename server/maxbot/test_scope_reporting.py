"""A separately delegated master can report scoped repairs without reassignment."""
from unittest.mock import patch
from decimal import Decimal
from django.test import TestCase,override_settings
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from accounts.models import AccessGrant
from projects import test_stock as fixture
from projects.models import ObjectAccess,ContractAccess
from repairs.access import visible_jobs,reportable_jobs,can_prepare
from repairs.models import Report,Attachment
from repairs.test_progress import photo
from . import tests as base,dialog,worker
from .models import Outbox,Draft

@override_settings(**base.CONFIG)
class ScopeReportingTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    bind=base.MaxTests.bind;event=base.MaxTests.event;fill=base.MaxTests.fill
    def setUp(self):
        base.MaxTests.setUp(self)
        self.job.assigned_to=self.leader;self.job.save(update_fields=['assigned_to'])
        defect=self.job.defects.get();defect.kilometre=Decimal('135.651');defect.save()
    def grant(self):return AccessGrant.objects.create(user=self.worker,permission='repairs.report_scope',granted_by=self.leader)
    def reply(self,event):return Outbox.objects.get(key=f'in:{event.pk}:reply')
    def test_read_only_overview_does_not_open_reporting(self):
        AccessGrant.objects.create(user=self.worker,permission='repairs.overview',granted_by=self.leader)
        self.assertIn(self.job,visible_jobs(self.worker));self.assertNotIn(self.job,dialog.available(self.worker))
        with self.assertRaises(PermissionDenied):dialog.start(self.binding,self.job,'repaired')
    def test_sections_jobs_photos_and_outgoing_recheck_use_same_scope(self):
        self.grant()
        row=self.reply(self.event('/работы'))
        self.assertIn('работы доступных вам участков',row.body['text'])
        self.assertIn(f'section:{self.section.pk}:0',str(row.body));self.assertTrue(worker.delivery_allowed(row))
        row=self.reply(self.event(f'section:{self.section.pk}:0'))
        self.assertIn('135.651',row.body['text']);self.assertIn(f'job:{self.job.pk}',str(row.body))
        self.assertTrue(worker.delivery_allowed(row))
        row=self.reply(self.event('/фото'));self.assertIn('135.651',row.body['text']);self.assertTrue(worker.delivery_allowed(row))
    def test_grinding_result_is_saved_with_actual_author_and_original_assignee(self):
        self.grant();self.event(f'begin:{self.job.pk}:repaired');draft=Draft.objects.get(action='repaired')
        self.fill(draft)
        with patch('maxbot.dialog.download_photo',side_effect=lambda _:photo()):self.event(photos=[{'type':'image','url':'https://synthetic.invalid/photo'}])
        self.event(f'review:{draft.pk}');self.event(f'confirm:{draft.pk}');self.event(f'confirm:{draft.pk}')
        report=Report.objects.get();self.assertEqual(report.author_id,self.worker.pk)
        self.assertEqual(report.context['actual_coating_length_m'],'2.400');self.assertEqual(Attachment.objects.count(),1)
        self.job.refresh_from_db();self.assertEqual(self.job.assigned_to_id,self.leader.pk);self.assertEqual(self.job.state,'reported')
        self.assertNotIn(self.job,dialog.available(self.worker))
    def test_daily_photos_are_saved_without_changing_assignee(self):
        self.grant();self.event(f'progress:select:{self.job.pk}');draft=Draft.objects.get(action='progress')
        with patch('maxbot.progress.download_photo',side_effect=lambda _:photo()):self.event(photos=[{'type':'image','url':'https://synthetic.invalid/photo'}])
        self.event(f'progress:save:{draft.pk}');report=Report.objects.get()
        self.assertEqual(report.author_id,self.worker.pk);self.assertEqual(report.attachments.count(),1)
        self.job.refresh_from_db();self.assertEqual(self.job.assigned_to_id,self.leader.pk)
    def test_scope_grant_alone_opens_lk_and_does_not_grant_preparation(self):
        self.grant();self.client.force_login(self.worker)
        self.assertContains(self.client.get(reverse('repair_detail',args=[self.job.pk])),'ТЕСТ125')
        self.assertEqual(self.client.get(reverse('repair_report',args=[self.job.pk,'repaired'])).status_code,200)
        self.assertContains(self.client.get(reverse('repairs')),'Результаты и фото по доступным участкам')
        self.assertFalse(can_prepare(self.worker))
        self.assertEqual(self.client.get(reverse('repair_reassign',args=[self.job.pk])).status_code,403)
    def test_missing_object_or_contract_prevents_listing_and_direct_selection(self):
        self.grant();ObjectAccess.objects.filter(user=self.worker,project_object=self.obj).delete()
        self.assertNotIn(self.job,dialog.available(self.worker))
        with self.assertRaises(PermissionDenied):dialog.start(self.binding,self.job,'repaired')
        ObjectAccess.objects.create(user=self.worker,project_object=self.obj,granted_by=self.leader)
        ContractAccess.objects.filter(user=self.worker,contract=self.contract).delete()
        self.assertNotIn(self.job,dialog.available(self.worker))
    def test_revocation_blocks_pending_reply_and_save_preserving_photos(self):
        grant=self.grant();row=self.reply(self.event(f'job:{self.job.pk}'))
        self.event(f'progress:select:{self.job.pk}');draft=Draft.objects.get(action='progress')
        with patch('maxbot.progress.download_photo',side_effect=lambda _:photo()):self.event(photos=[{'type':'image','url':'https://synthetic.invalid/photo'}])
        grant.delete();self.assertFalse(worker.delivery_allowed(row))
        self.event(f'progress:save:{draft.pk}')
        self.assertFalse(Report.objects.exists());self.assertEqual(draft.photos.count(),1)
    def test_grant_does_not_expand_pto_subcontractor_or_customer_reporting(self):
        self.grant()
        for role in ('pto','subcontractor','customer','other'):
            self.worker.role=role;self.worker.save(update_fields=['role'])
            self.assertNotIn(self.job,reportable_jobs(self.worker))
    def test_archived_object_contract_and_closed_jobs_stay_out_of_bot(self):
        self.grant()
        for state in ('reported','confirmed','unrepairable'):
            self.job.state=state;self.job.save(update_fields=['state']);self.assertNotIn(self.job,dialog.available(self.worker))
        self.job.state='planned';self.job.save(update_fields=['state']);self.obj.status='archived';self.obj.save()
        self.assertNotIn(self.job,dialog.available(self.worker))
    def test_crew_assignment_period_is_still_required(self):
        from datetime import timedelta
        from django.utils import timezone
        self.grant();assignment=fixture.StockTests.crew(self)
        today=timezone.localdate();assignment.start_date=today-timedelta(days=10);assignment.end_date=today-timedelta(days=1);assignment.save()
        self.job.crew_assignment=assignment;self.job.save(update_fields=['crew_assignment'])
        self.assertNotIn(self.job,dialog.available(self.worker))
        assignment.end_date=None;assignment.start_date=today+timedelta(days=1);assignment.save()
        self.assertNotIn(self.job,dialog.available(self.worker))
        assignment.start_date=today;assignment.save()
        self.assertIn(self.job,dialog.available(self.worker))

    def test_new_login_and_max_relink_after_permission_change_keep_scope(self):
        from . import links
        self.grant();self.worker.session_version+=1;self.worker.save(update_fields=['session_version'])
        self.binding.refresh_from_db();self.assertFalse(links.current(self.binding))
        self.binding=self.bind();self.assertTrue(links.current(self.binding))
        row=self.reply(self.event('/работы'))
        self.assertIn(f'section:{self.section.pk}:0',str(row.body));self.assertTrue(worker.delivery_allowed(row))

    def test_no_default_scope_reporting_grants(self):
        from accounts.presets import PRESETS
        self.assertTrue(all('repairs.report_scope' not in profile['permissions'] for profile in PRESETS.values()))
