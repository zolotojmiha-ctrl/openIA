"""Synthetic regressions for gallery identity and MAX temporary file ownership."""
from django.test import TestCase
from django.urls import reverse
from django.db import transaction
from documents.storage import private_path,store_file,validate_upload
from maxbot.models import Binding,Draft,Photo
from maxbot.draft_files import release
from . import test_progress as fixtures
from .models import Attachment
from .photos import gallery
from .services import report_work


class PhotoEvidenceTests(TestCase):
    setUpTestData=classmethod(fixtures.ProgressTests.setUpTestData.__func__)
    setUp=fixtures.ProgressTests.setUp
    make_job=fixtures.ProgressTests.make_job
    data=fixtures.ProgressTests.data
    def test_gallery_shows_same_bytes_once_and_preserves_report_links(self):
        job=self.make_job()
        for _ in range(2):
            job.refresh_from_db();report_work(self.worker,job.pk,'note',self.data(job))
        self.assertEqual(Attachment.objects.filter(report__job=job).count(),2)
        items=gallery(job);self.assertEqual(len(items),1);self.assertEqual(len(items[0]['reports']),2)
        self.client.force_login(self.worker)
        response=self.client.get(reverse('repair_detail',args=[job.pk]))
        self.assertContains(response,'<img ',count=1)
        for report in job.reports.all():self.assertContains(response,f'report={report.pk}')

    def test_document_versions_keep_independent_storage_keys(self):
        job=self.make_job();photo=report_work(self.worker,job.pk,'note',self.data(job)).attachments.get()
        data=validate_upload(fixtures.photo())
        first=store_file(data);second=store_file(data)
        self.assertNotEqual(first,second);self.assertNotEqual(first,photo.storage_key)

    def test_focus_report_opens_correct_history_page(self):
        job=self.make_job()
        old=report_work(self.worker,job.pk,'note',self.data(job))
        for _ in range(21):
            job.refresh_from_db();report_work(self.worker,job.pk,'note',self.data(job))
        self.client.force_login(self.worker)
        response=self.client.get(reverse('repair_detail',args=[job.pk]),{'report':str(old.pk)})
        self.assertEqual(response.context['history_page'].number,2)
        self.assertContains(response,f'id="report-{old.pk}"')
        other=self.make_job()
        self.assertEqual(self.client.get(reverse('repair_detail',args=[other.pk]),{'report':str(old.pk)}).status_code,404)

    def draft(self,job):
        binding=Binding.objects.create(user=self.worker,max_user=99101,chat=99102,session_version=self.worker.session_version)
        from django.utils import timezone
        return Draft.objects.create(binding=binding,job=job,action='progress',expires=timezone.now())

    def test_submitted_draft_cleanup_preserves_evidence(self):
        job=self.make_job();report=report_work(self.worker,job.pk,'note',self.data(job))
        draft=self.draft(job);data=validate_upload(fixtures.photo());key=store_file(dict(data,content_type='application/octet-stream'))
        Photo.objects.create(draft=draft,storage_key=key,**{k:data[k] for k in ('name','sha256','size')})
        evidence=report.attachments.get()
        with self.captureOnCommitCallbacks(execute=True):release(draft)
        self.assertFalse(private_path(key).exists());self.assertTrue(private_path(evidence.storage_key).exists())
        self.assertFalse(draft.photos.exists())

    def test_failed_transaction_keeps_draft_photos(self):
        job=self.make_job();draft=self.draft(job);data=validate_upload(fixtures.photo());key=store_file(dict(data,content_type='application/octet-stream'))
        Photo.objects.create(draft=draft,storage_key=key,**{k:data[k] for k in ('name','sha256','size')})
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    release(draft);raise ValueError('synthetic rollback')
            except ValueError:pass
        self.assertTrue(draft.photos.exists());self.assertTrue(private_path(key).exists())


class PhotoManagementTests(TestCase):
    setUpTestData=classmethod(fixtures.ProgressTests.setUpTestData.__func__)
    setUp=fixtures.ProgressTests.setUp
    make_job=fixtures.ProgressTests.make_job
    data=fixtures.ProgressTests.data

    def saved(self):
        job=self.make_job();return report_work(self.worker,job.pk,'note',self.data(job)).attachments.get()

    def grant(self,key):
        from accounts.models import AccessGrant
        return AccessGrant.objects.create(user=self.worker,permission=key,granted_by=self.leader)

    def test_author_cannot_edit_until_explicit_grant_and_delete_is_separate(self):
        from django.core.exceptions import PermissionDenied,ValidationError
        from . import photos
        photo=self.saved()
        with self.assertRaises(PermissionDenied):photos.preview(self.worker,photo.pk,'edit',name='changed.png')
        grant=self.grant('repairs.photos.edit')
        item=photos.preview(self.worker,photo.pk,'edit',name='changed.png',caption='TEST caption')
        with self.assertRaises(PermissionDenied):photos.preview(self.worker,photo.pk,'delete')
        photos.confirm(self.worker,item.pk);photo.refresh_from_db();self.assertEqual(photo.name,'changed.png')
        self.assertEqual(photo.versions.get().manifest['name'],'TEST.png')
        grant.delete()
        with self.assertRaises(ValidationError):photos.confirm(self.worker,item.pk)

    def test_revoked_scope_and_concurrent_change_reject_preview_confirmation(self):
        from django.core.exceptions import ValidationError,PermissionDenied
        from . import photos
        photo=self.saved();self.grant('repairs.photos.edit')
        old=photos.preview(self.worker,photo.pk,'edit',name='old-preview.png')
        new=photos.preview(self.leader,photo.pk,'edit',name='new-preview.png')
        photos.confirm(self.leader,new.pk)
        with self.assertRaises(ValidationError):photos.confirm(self.worker,old.pk)
        photo.refresh_from_db();fresh=photos.preview(self.worker,photo.pk,'edit',name='fresh.png')
        photo.report.job.assigned_to=self.leader;photo.report.job.save()
        with self.assertRaises(PermissionDenied):photos.confirm(self.worker,fresh.pk)

    def test_delete_shared_file_keeps_other_report_and_deletes_last_reference(self):
        from . import photos
        from documents.storage import discard_file
        photo=self.saved();job=self.make_job()
        second=report_work(self.worker,job.pk,'note',self.data(job)).attachments.get()
        self.assertEqual(photo.storage_key,second.storage_key);key=photo.storage_key
        item=photos.preview(self.leader,photo.pk,'delete')
        with self.captureOnCommitCallbacks(execute=True):photos.confirm(self.leader,item.pk)
        self.assertTrue(private_path(key).exists());self.assertFalse(discard_file(key))
        item=photos.preview(self.leader,second.pk,'delete')
        with self.captureOnCommitCallbacks(execute=True):photos.confirm(self.leader,item.pk)
        self.assertFalse(private_path(key).exists())
        self.client.force_login(self.leader)
        self.assertEqual(self.client.get(reverse('repair_attachment',args=[photo.pk])).status_code,404)
        self.assertEqual(self.client.get(reverse('repair_photo_history',args=[photo.pk])).status_code,200)

    def test_accepted_report_replacement_keeps_original_version_and_cancel_is_noop(self):
        from . import photos
        import io
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        photo=self.saved();original=photo.storage_key
        job=photo.report.job;job.state='confirmed';job.save()
        data=io.BytesIO();Image.new('RGB',(10,10),'red').save(data,'PNG')
        item=photos.preview(self.leader,photo.pk,'edit',name='replacement.png',upload=SimpleUploadedFile('replacement.png',data.getvalue()))
        photos.confirm(self.leader,item.pk);photo.refresh_from_db()
        self.assertNotEqual(photo.storage_key,original);self.assertTrue(private_path(original).exists())
        self.assertEqual(photo.versions.get().storage_key,original)
        cancelled=photos.preview(self.leader,photo.pk,'delete');photos.cancel(self.leader,cancelled.pk)
        photo.refresh_from_db();self.assertIsNone(photo.deleted_at)

    def test_reuse_attaches_existing_photo_without_copy_and_rejects_foreign_work(self):
        import uuid
        from .forms import ReportForm
        from . import photos
        from django.core.exceptions import ValidationError
        photo=self.saved();job=photo.report.job;job.refresh_from_db()
        job.method='grind';job.save()
        initial=ReportForm(job=job,action='result',user=self.worker).initial
        form=ReportForm({'reuse_snapshot':initial.get('reuse_snapshot',''),'token':uuid.uuid4(),'expected_version':job.version,'performed_date':self.today,'outcome':'complete','method':'grind','actual_coating_length_m':'1','reuse_photos':'all'},job=job,action='result',user=self.worker)
        self.assertTrue(form.is_valid(),form.errors)
        report=report_work(self.worker,job.pk,'result',form.cleaned_data)
        self.assertEqual(list(report.reused_photos.all()),[photo]);self.assertFalse(report.attachments.exists())
        self.assertEqual(Attachment.objects.count(),1)
        other=self.make_job()
        data=self.data(other);data['reuse_photos']=[photo]
        with self.assertRaises(ValidationError):report_work(self.worker,other.pk,'note',data)


class PhotoCleanupTests(TestCase):
    setUpTestData=classmethod(fixtures.ProgressTests.setUpTestData.__func__)
    setUp=fixtures.ProgressTests.setUp
    make_job=fixtures.ProgressTests.make_job
    data=fixtures.ProgressTests.data
    def saved(self):
        job=self.make_job();return report_work(self.worker,job.pk,'note',self.data(job)).attachments.get()

    def test_external_delete_failure_preserves_local_file_and_retry_queue(self):
        from unittest.mock import patch,Mock
        from . import photos,photo_cleanup
        from .models import PhotoPurge
        from projects.models import PhotoArchive
        from projects.yandex_disk import ROOT,DiskError
        from django.utils import timezone
        photo=self.saved();key=photo.storage_key
        PhotoArchive.objects.create(storage_key=key,sha256=photo.sha256,size=photo.size,remote_path=ROOT+'/photos/'+key.replace('/','-')+'-'+photo.sha256,verified_at=timezone.now())
        operation=photos.preview(self.leader,photo.pk,'delete')
        with self.captureOnCommitCallbacks(execute=True):photos.confirm(self.leader,operation.pk)
        self.assertTrue(private_path(key).exists())
        with patch('projects.photo_archive.client',side_effect=DiskError('synthetic error')):photo_cleanup.step()
        purge=PhotoPurge.objects.get();self.assertEqual(purge.state,'error');self.assertTrue(private_path(key).exists())
        purge.available_at=timezone.now();purge.save()
        disk=Mock();disk.request.side_effect=[{},FileNotFoundError()]
        with patch('projects.photo_archive.client',return_value=disk):photo_cleanup.step()
        self.assertFalse(private_path(key).exists());self.assertFalse(PhotoArchive.objects.exists())
        purge.refresh_from_db();self.assertEqual(purge.state,'done')

    def test_duplicate_preview_is_non_destructive_and_stale_plan_is_rejected(self):
        from . import photo_cleanup
        from django.core.exceptions import ValidationError
        photo=self.saved();data=validate_upload(fixtures.photo());key=store_file(dict(data,content_type='application/octet-stream'))
        other=Attachment.objects.create(report=photo.report,storage_key=key,**{field:data[field] for field in ('name','content_type','sha256','size')})
        plan=photo_cleanup.duplicate_plan();self.assertEqual(len(plan),1);self.assertTrue(private_path(key).exists())
        with self.assertRaises(ValidationError):photo_cleanup.consolidate('0'*64)
        with self.captureOnCommitCallbacks(execute=True):photo_cleanup.consolidate(photo_cleanup.plan_digest(plan))
        photo.refresh_from_db();other.refresh_from_db();self.assertEqual(photo.storage_key,other.storage_key)
        self.assertEqual(len([path for path in __import__('pathlib').Path(self.root.name).glob('*/*') if path.is_file()]),1)
