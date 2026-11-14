from unittest.mock import patch
from datetime import timedelta
from django.test import TestCase,override_settings
from django.utils import timezone
from projects import test_stock as fixture
from repairs.models import Report
from repairs.test_progress import photo
from . import tests as base
from .models import Draft,Outbox


@override_settings(**base.CONFIG)
class ProgressBotTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    setUp=base.MaxTests.setUp
    bind=base.MaxTests.bind;event=base.MaxTests.event
    def begin(self):
        self.event('progress:select:'+str(self.job.pk))
        return Draft.objects.get(action='progress',state='photos')
    def add_photo(self):
        with patch('maxbot.progress.download_photo',side_effect=lambda _:photo()):self.event(photos=[{'type':'image','url':'https://verified.invalid/TEST'}])
    def test_minimal_save_retry_and_next_day_preserve_photos(self):
        draft=self.begin();self.assertEqual(draft.raw['performed_date'],str(timezone.localdate()))
        self.add_photo();self.event('progress:save:'+str(draft.pk))
        report=Report.objects.get();self.assertEqual(report.context['progress'],{'stages':[]})
        self.event('progress:save:'+str(draft.pk));self.assertEqual(Report.objects.count(),1)
        self.event('progress:select:'+str(self.job.pk));self.add_photo()
        second=Draft.objects.get(action='progress',state='photos');self.event('progress:save:'+str(second.pk))
        self.assertEqual(Report.objects.count(),2)
        self.assertEqual(report.attachments.count(),1)
    def test_date_stages_caption_and_multiple_choices(self):
        draft=self.begin()
        self.event(f'progress:toggle:{draft.pk}:excavation');self.event(f'progress:toggle:{draft.pk}:ddk')
        self.event(f'progress:date:{draft.pk}');day=timezone.localdate()-timedelta(days=1);self.event(day.strftime('%d.%m.%y'))
        self.event('TEST: три котлована');self.add_photo();self.event('progress:save:'+str(draft.pk))
        report=Report.objects.get();self.assertEqual(report.performed_date,day);self.assertEqual(report.note,'TEST: три котлована')
        self.assertEqual(report.context['progress']['stages'],['excavation','ddk'])
    def test_pending_photos_cannot_be_silently_replaced_by_final_report(self):
        draft=self.begin();self.add_photo();self.event(f'begin:{self.job.pk}:repaired')
        draft.refresh_from_db();self.assertEqual(draft.state,'photos');self.assertEqual(draft.photos.count(),1)
        self.assertEqual(Draft.objects.count(),1);self.assertFalse(Report.objects.exists())
    def test_progress_notification_is_not_a_completion_notice(self):
        self.bind(sender=301,chat=401,user=self.leader)
        draft=self.begin();self.add_photo();self.event('progress:save:'+str(draft.pk))
        notice=Outbox.objects.get(key__startswith='leader:')
        self.assertIn('Фото хода работ',notice.body['text']);self.assertNotIn('на проверке',notice.body['text'])

    def test_version_conflict_keeps_photos_until_user_rechecks_work(self):
        draft=self.begin();self.add_photo()
        self.job.version+=1;self.job.save()
        self.event('progress:save:'+str(draft.pk));self.assertFalse(Report.objects.exists())
        draft.refresh_from_db();self.assertEqual(draft.photos.count(),1)
        self.event('progress:refresh:'+str(draft.pk));self.event('progress:save:'+str(draft.pk))
        self.assertEqual(Report.objects.count(),1)

    def test_list_has_assigned_kilometres_before_creating_draft(self):
        event=self.event('/фото');row=Outbox.objects.get(key=f'in:{event.pk}:reply')
        self.assertIn(f'worknav:jobs:photos:{self.section.pk}:0',str(row.body))
        event=self.event(f'worknav:jobs:photos:{self.section.pk}:0');row=Outbox.objects.get(key=f'in:{event.pk}:reply')
        self.assertIn('№ТЕСТ125',str(row.body))
        self.assertIn(f'progress:select:{self.job.pk}',str(row.body))
        self.assertFalse(Draft.objects.exists())

    def test_expired_draft_can_be_explicitly_resumed_with_photos(self):
        draft=self.begin();self.add_photo();draft.expires=timezone.now()-timedelta(minutes=1);draft.save(update_fields=['expires'])
        self.event('/фото');self.event('progress:select:'+str(self.job.pk))
        draft.refresh_from_db();self.assertGreater(draft.expires,timezone.now());self.assertEqual(draft.photos.count(),1)
        self.event('progress:save:'+str(draft.pk));self.assertEqual(Report.objects.count(),1)

    def test_reassignment_blocks_save_and_preserves_draft_evidence(self):
        draft=self.begin();self.add_photo();self.job.assigned_to=self.leader;self.job.save()
        self.event('progress:save:'+str(draft.pk));self.assertFalse(Report.objects.exists())
        draft.refresh_from_db();self.assertEqual(draft.photos.count(),1)

    def test_leader_menu_links_to_progress_timeline(self):
        self.bind(sender=301,chat=401,user=self.leader)
        draft=self.begin();self.add_photo();self.event('progress:save:'+str(draft.pk))
        event=self.event('leader:progress:0',sender=301,chat=401)
        row=Outbox.objects.get(key=f'in:{event.pk}:reply')
        self.assertIn('Добавить фотографии',str(row.body));self.assertIn('Полученные фотоотчёты',str(row.body))
        self.event('worknav:sections:history:0',sender=301,chat=401)
        self.event(f'worknav:jobs:history:{self.section.pk}:0',sender=301,chat=401)
        event=self.event(f'worknav:history:{self.job.pk}:0',sender=301,chat=401);row=Outbox.objects.get(key=f'in:{event.pk}:reply')
        self.assertIn('Фото за',row.body['text']);self.assertIn('#progress-timeline',str(row.body))


    def test_reopening_bot_does_not_erase_unsaved_caption(self):
        draft=self.begin();self.event('TEST подпись')
        self.event('',kind='bot_started')
        draft.refresh_from_db();self.assertEqual(draft.raw['note'],'TEST подпись')
