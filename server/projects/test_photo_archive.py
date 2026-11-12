import hashlib,io,tempfile
from types import SimpleNamespace
from unittest.mock import Mock,patch
from django.test import TestCase,override_settings
from documents.storage import store_file,verified_file,private_path
from .models import PhotoArchive
from . import photo_archive
from .yandex_disk import Client,DiskError


class PhotoArchiveTests(TestCase):
    def test_archive_verifies_before_recording_and_keeps_local_original(self):
        with tempfile.TemporaryDirectory() as root,override_settings(PRIVATE_DOCUMENT_ROOT=root):
            data=b'TEST photo bytes';key=store_file({'content':data})
            item=SimpleNamespace(storage_key=key,sha256=hashlib.sha256(data).hexdigest(),size=len(data))
            disk=Mock()
            with patch('projects.photo_archive.client',return_value=disk):row=photo_archive.archive_one(item)
            disk.verify.assert_called_once_with(row.remote_path,item.sha256,item.size)
            self.assertEqual(private_path(key).read_bytes(),data)
            self.assertEqual(PhotoArchive.objects.count(),1)
            disk.verify.side_effect=DiskError('TEST corrupt remote')
            PhotoArchive.objects.all().delete()
            with patch('projects.photo_archive.client',return_value=disk),self.assertRaises(DiskError):photo_archive.archive_one(item)
            self.assertFalse(PhotoArchive.objects.exists());self.assertTrue(private_path(key).exists())

    def test_authenticated_download_can_fall_back_to_verified_private_remote(self):
        with tempfile.TemporaryDirectory() as root,override_settings(PRIVATE_DOCUMENT_ROOT=root):
            data=b'TEST data';key=store_file({'content':data})
            item=SimpleNamespace(storage_key=key,sha256=hashlib.sha256(data).hexdigest(),size=len(data))
            PhotoArchive.objects.create(storage_key=key,sha256=item.sha256,size=item.size,remote_path='app:/armada/photos/test',verified_at=__import__('django.utils.timezone',fromlist=['now']).now())
            private_path(key).unlink();disk=Mock();disk.download_verified.return_value=io.BytesIO(data)
            with patch('projects.photo_archive.client',return_value=disk):
                with verified_file(item) as f:self.assertEqual(f.read(),data)
            disk.download_verified.assert_called_once_with('app:/armada/photos/test',item.sha256,item.size)

    def test_remote_stream_rejects_wrong_digest_and_wrong_host(self):
        response=Mock();response.__enter__=Mock(return_value=io.BytesIO(b'wrong'));response.__exit__=Mock(return_value=False)
        disk=Client('x'*30,opener=Mock(open=Mock(return_value=response)))
        with patch.object(disk,'request',return_value={'href':'https://downloader.disk.yandex.net/test'}):
            with self.assertRaises(DiskError):disk.download_verified('app:/armada/photos/test','a'*64,5)
        with patch.object(disk,'request',return_value={'href':'https://example.org/test'}):
            with self.assertRaises(DiskError):disk.download_verified('app:/armada/photos/test','a'*64,5)


from . import test_stock as fixture_data

class PhotoPruningTests(TestCase):
    setUpTestData=classmethod(fixture_data.StockTests.setUpTestData.__func__)
    make_job=fixture_data.StockTests.make_job

    def test_local_removal_requires_age_full_backup_and_second_readback(self):
        from datetime import timedelta
        from django.utils import timezone
        from repairs.models import Report,Attachment
        from .models import DiskBackupJob
        import uuid
        with tempfile.TemporaryDirectory() as root,override_settings(PRIVATE_DOCUMENT_ROOT=root):
            data=b'TEST old photo';key=store_file({'content':data});job=self.make_job()
            report=Report.objects.create(job=job,author=self.worker,performed_date=self.today,kind='note')
            Report.objects.filter(pk=report.pk).update(created_at=timezone.now()-timedelta(days=8))
            item=Attachment.objects.create(report=report,storage_key=key,name='TEST.jpg',content_type='image/jpeg',sha256=hashlib.sha256(data).hexdigest(),size=len(data))
            row=PhotoArchive.objects.create(storage_key=key,sha256=item.sha256,size=item.size,remote_path='app:/armada/photos/TEST',verified_at=timezone.now())
            disk=Mock()
            with patch('projects.photo_archive.client',return_value=disk):self.assertFalse(photo_archive.prune_one())
            self.assertTrue(private_path(key).exists());disk.verify.assert_not_called()
            DiskBackupJob.objects.create(config_version=1,key_fingerprint='TEST',public_key='TEST',state='verified',started_at=timezone.now()-timedelta(days=1),verified_at=timezone.now())
            disk.verify.side_effect=DiskError('TEST failed check')
            with patch('projects.photo_archive.client',return_value=disk),self.assertRaises(DiskError):photo_archive.prune_one()
            self.assertTrue(private_path(key).exists())
            disk.verify.side_effect=None
            with patch('projects.photo_archive.client',return_value=disk):self.assertTrue(photo_archive.prune_one())
            self.assertFalse(private_path(key).exists());row.refresh_from_db();self.assertIsNotNone(row.local_removed_at)
