from unittest.mock import patch,MagicMock
from django.test import TestCase,override_settings
from django.core.exceptions import PermissionDenied
from accounts.models import AccessGrant
from repairs.models import PhotoOperation
from repairs.services import report_work
from repairs import test_progress as progress_fixture
from . import tests as fixture
from .models import Outbox


@override_settings(**fixture.CONFIG)
class PhotoManagementBotTests(TestCase):
    setUpTestData=classmethod(fixture.MaxTests.setUpTestData.__func__)
    setUp=fixture.MaxTests.setUp
    event=fixture.MaxTests.event
    bind=fixture.MaxTests.bind
    def saved(self):
        data=progress_fixture.ProgressTests.data(self,self.job)
        return report_work(self.worker,self.job.pk,'note',data).attachments.get()
    def grant(self,key):return AccessGrant.objects.create(user=self.worker,permission=key,granted_by=self.leader)

    def test_native_caption_preview_confirm_cancel_and_revocation(self):
        photo=self.saved();grant=self.grant('repairs.photos.edit')
        self.event(f'photos:caption:{photo.pk}');self.event('SYNTHETIC caption')
        operation=PhotoOperation.objects.get();photo.refresh_from_db();self.assertEqual(photo.caption,'')
        self.event(f'photos:confirm:{operation.pk}');photo.refresh_from_db();self.assertEqual(photo.caption,'SYNTHETIC caption')
        self.event(f'photos:name:{photo.pk}');self.event('new-name.png');second=PhotoOperation.objects.exclude(pk=operation.pk).get()
        grant.delete();self.event(f'photos:confirm:{second.pk}');photo.refresh_from_db();self.assertEqual(photo.name,'TEST.png')
        self.event(f'photos:cancel:{second.pk}');second.refresh_from_db();self.assertEqual(second.state,'cancelled')

    def test_foreman_cannot_forge_delete_callback_and_edit_does_not_grant_delete(self):
        photo=self.saved();self.grant('repairs.photos.edit')
        self.event(f'photos:delete:{photo.pk}');self.assertFalse(PhotoOperation.objects.exists())
        self.grant('repairs.photos.delete');self.event(f'photos:delete:{photo.pk}')
        operation=PhotoOperation.objects.get();self.event(f'photos:cancel:{operation.pk}')
        photo.refresh_from_db();self.assertIsNone(photo.deleted_at)
        self.event('photos:select:00000000-0000-0000-0000-000000000000')

    def test_native_file_replacement_and_view_prepare_private_media(self):
        photo=self.saved();self.grant('repairs.photos.edit')
        event=self.event(f'photos:select:{photo.pk}')
        row=Outbox.objects.get(key=f'in:{event.pk}:reply');self.assertEqual(row.body['_repair_photo']['id'],str(photo.pk))
        self.event(f'photos:file:{photo.pk}')
        with patch('maxbot.transport.download_photo',side_effect=lambda _:progress_fixture.photo()):self.event(photos=[{'type':'image','url':'https://synthetic.invalid/image'}])
        operation=PhotoOperation.objects.get();self.event(f'photos:confirm:{operation.pk}')
        photo.refresh_from_db();self.assertEqual(photo.revision,2)

    def test_upload_preview_uses_verified_bytes_and_does_not_forward_api_token(self):
        from . import transport
        photo=self.saved();body={'text':'TEST','_repair_photo':{'id':str(photo.pk),'revision':photo.revision}}
        response=MagicMock();response.__enter__.return_value.read.return_value=b'{"photos":{"test":{"token":"synthetic"}}}'
        opener=MagicMock();opener.open.return_value=response
        with patch.object(transport,'api',return_value={'url':'https://upload.max.ru/test'}),patch.object(transport.socket,'getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]),patch.object(transport,'build_opener',return_value=opener):
            result=transport.prepare_photo_body(body)
        self.assertNotIn('_repair_photo',result);self.assertEqual(result['attachments'][0]['type'],'image')
        request=opener.open.call_args.args[0];self.assertFalse(request.has_header('Authorization'))
        with patch.object(transport,'api',return_value={'url':'https://evil.invalid/upload'}):
            with self.assertRaises(transport.DeliveryError):transport.prepare_photo_body(body)

    def test_failed_preview_upload_delivers_readable_fallback_without_double_message(self):
        from . import worker,transport
        photo=self.saved();event=self.event(f'photos:select:{photo.pk}')
        row=Outbox.objects.get(key=f'in:{event.pk}:reply')
        Outbox.objects.exclude(pk=row.pk).update(state='cancelled')
        with patch.object(transport,'prepare_photo_body',side_effect=transport.DeliveryError('failed')),patch.object(transport,'send') as send:
            worker.deliver(row.pk)
        row.refresh_from_db();self.assertEqual(row.state,'sent');send.assert_called_once()
        body=send.call_args.args[1];self.assertNotIn('_repair_photo',body)
        self.assertIn('Не удалось показать изображение',body['text']);self.assertIn('Повторить просмотр',str(body))
