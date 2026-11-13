import uuid
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import ValidationError,PermissionDenied
from cryptography.fernet import Fernet
from accounts.models import User
from . import tests as fixture,services,trash
from .access import boxes
from .models import TrashEntry,Mailbox

RAW=b'From: sender@example.org\r\nTo: shared@armada-sibir.ru\r\nSubject: Old letter\r\nDate: Fri, 01 Jan 1999 12:00:00 +0000\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nLetter body'

class MemoryIMAP:
    capabilities=(b'IMAP4rev1',b'UIDPLUS',b'MOVE')
    def __init__(self):
        self.data={'INBOX':{1:RAW,2:RAW,3:RAW},'Trash':{},'Sent':{}}
        self.generations={'INBOX':7,'Trash':8,'Sent':9};self.folder='INBOX';self.commands=[];self.deleted={}
    def list(self):return 'OK',[b'(\\HasNoChildren) "/" "INBOX"',b'(\\Trash) "/" "Trash"',b'(\\Sent) "/" "Sent"']
    def select(self,name,readonly=True):self.folder=name.strip('"');return 'OK',[str(len(self.data[self.folder])).encode()]
    def response(self,name):return name,[str(self.generations[self.folder]).encode()]
    def uid(self,command,*args):
        self.commands.append((self.folder,command,args));rows=self.data[self.folder]
        if command=='SEARCH':
            ids=set(rows)
            if 'UID' in args:ids&={int(v) for v in args[-1].split(',')}
            return 'OK',[' '.join(str(v) for v in sorted(ids)).encode()]
        ids={int(v) for v in (args[0].decode() if isinstance(args[0],bytes) else str(args[0])).split(',')} if args else set()
        if command in ('MOVE','COPY'):
            target=self.data[args[1].strip('"')]
            for uid in sorted(ids&rows.keys()):
                target[max(target,default=100)+1]=rows[uid]
                if command=='MOVE':del rows[uid]
        elif command=='STORE':
            if '\\Deleted' in args[-1]:self.deleted.setdefault(self.folder,set()).update(ids)
        elif command=='EXPUNGE':
            for uid in ids&self.deleted.get(self.folder,set()):rows.pop(uid,None)
        elif command=='FETCH':
            result=[]
            for uid in sorted(ids):
                raw=rows.get(uid)
                if raw is None:continue
                if 'RFC822.SIZE' in args[-1]:result.append(f'1 (UID {uid} RFC822.SIZE {len(raw)})'.encode())
                else:result.extend([(f'1 (UID {uid} FLAGS () BODY[] {{{len(raw)}}}'.encode(),raw),b')'])
            return 'OK',result
        return 'OK',[b'']
    @contextmanager
    def connection(self,box):yield self


@override_settings(ARMADA_MAIL_KEY=Fernet.generate_key().decode())
class SharedTrashTests(TestCase):
    setUpTestData=classmethod(fixture.MailTests.setUpTestData.__func__)
    box=fixture.MailTests.box
    def setUp(self):
        self.mailbox=self.box();self.server=MemoryIMAP()
        self.patcher=patch('postoffice.transport.connect',self.server.connection);self.patcher.start();self.addCleanup(self.patcher.stop)

    def selection(self,folder='INBOX',uids=('1','2')):
        return trash.token(self.worker,self.mailbox,folder,str(self.server.generations[folder]),uids)

    def test_multiple_members_revocation_no_duplicates_and_no_extra_rights(self):
        box=services.configure(self.leader,self.mailbox.pk,self.mailbox.version,'',None,None,True,members=[self.worker,self.other])
        self.assertIsNone(box.owner_id);self.assertEqual(set(box.members.all()),{self.worker,self.other})
        for user in (self.worker,self.other,self.leader,self.second):self.assertTrue(boxes(user).filter(pk=box.pk).exists())
        # Legacy and explicit access together must not duplicate counts.
        Mailbox.objects.filter(pk=box.pk).update(owner=self.worker)
        self.assertEqual(boxes(self.worker).filter(pk=box.pk).count(),1)
        box=services.configure(self.leader,box.pk,box.version,'',None,None,True,members=[self.other])
        self.assertFalse(boxes(self.worker).filter(pk=box.pk).exists())
        self.assertTrue(boxes(self.other).filter(pk=box.pk).exists())
        with self.assertRaises(PermissionDenied):services.configure(self.other,box.pk,box.version,'',None,None,True,members=[self.worker])

    def test_configure_ui_preselects_legacy_owner_and_saves_multiple(self):
        self.client.force_login(self.leader);url=reverse('mailbox_configure',args=[self.mailbox.pk])
        page=self.client.get(url);self.assertIn(self.worker.pk,page.context['form'].initial['members'])
        response=self.client.post(url,{'version':self.mailbox.version,'members':[self.worker.pk,self.other.pk],'enabled':'on'})
        self.assertEqual(response.status_code,302);self.assertEqual(self.mailbox.members.count(),2)
        self.client.force_login(self.other);self.assertEqual(self.client.get(url).status_code,403)
        self.assertContains(self.client.get(reverse('mail')),self.mailbox.address)

    def test_bulk_move_restore_and_repeat_do_not_delete_other_messages(self):
        selection=self.selection();self.client.force_login(self.worker)
        response=self.client.post(reverse('mail_bulk_action',args=[self.mailbox.pk]),{'selection':selection,'uids':['1','2'],'folder':'INBOX','action':'trash'})
        self.assertEqual(response.status_code,302);self.assertEqual(set(self.server.data['INBOX']),{3})
        self.assertEqual(len(self.server.data['Trash']),2);self.assertEqual(TrashEntry.objects.count(),2)
        with self.assertRaises(ValidationError):trash.apply_selection(self.worker,self.mailbox.pk,selection,['1','2'],'trash')
        ids=[str(v) for v in self.server.data['Trash']]
        trash.apply_selection(self.worker,self.mailbox.pk,self.selection('Trash',ids),ids,'restore')
        self.assertEqual(len(self.server.data['INBOX']),3);self.assertEqual(TrashEntry.objects.count(),0)

    def test_selection_checks_actor_membership_folder_generation_and_uid(self):
        selection=self.selection()
        with self.assertRaises(PermissionDenied):trash.apply_selection(self.leader,self.mailbox.pk,selection,['1'],'trash')
        with self.assertRaises(ValidationError):trash.apply_selection(self.worker,self.mailbox.pk,selection,['3'],'trash')
        self.server.generations['INBOX']=99
        with self.assertRaises(ValidationError):trash.apply_selection(self.worker,self.mailbox.pk,selection,['1'],'trash')
        self.assertEqual(len(self.server.data['INBOX']),3)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse('mail_bulk_action',args=[self.mailbox.pk]),{'selection':selection,'uids':['1'],'action':'trash'}).status_code,404)

    def test_old_letter_retained_for_full_30_days_and_targeted_expunge_only(self):
        trash.apply_selection(self.worker,self.mailbox.pk,self.selection(),['1'],'trash')
        entry=TrashEntry.objects.get();start=entry.trashed_at
        self.assertEqual(trash.cleanup_box(self.mailbox.pk,start+timedelta(days=30)-timedelta(seconds=1)),0)
        # Another client has marked a newer letter Deleted: global EXPUNGE would lose it.
        self.server.data['Trash'][999]=RAW;self.server.deleted['Trash']={999}
        self.assertEqual(trash.cleanup_box(self.mailbox.pk,start+timedelta(days=30)),1)
        self.assertEqual(set(self.server.data['Trash']),{999})
        self.assertIn(('Trash','EXPUNGE',(str(entry.uid),)),self.server.commands)
        self.assertEqual(TrashEntry.objects.get().uid,999)

    def test_existing_trash_and_uidvalidity_reset_get_fresh_30_days(self):
        self.server.data['Trash'][50]=RAW;now=timezone.now()
        self.assertEqual(trash.cleanup_box(self.mailbox.pk,now),0)
        self.assertEqual(TrashEntry.objects.get().trashed_at,now)
        self.server.generations['Trash']+=1
        self.assertEqual(trash.cleanup_box(self.mailbox.pk,now+timedelta(days=31)),0)
        self.assertEqual(TrashEntry.objects.get().trashed_at,now+timedelta(days=31))

    def test_restore_and_redelete_resets_deadline(self):
        trash.apply_selection(self.worker,self.mailbox.pk,self.selection(),['1'],'trash')
        old=TrashEntry.objects.get();TrashEntry.objects.filter(pk=old.pk).update(trashed_at=timezone.now()-timedelta(days=29))
        uid=str(old.uid);trash.apply_selection(self.worker,self.mailbox.pk,self.selection('Trash',[uid]),[uid],'restore')
        restored=str(max(self.server.data['INBOX']))
        trash.apply_selection(self.worker,self.mailbox.pk,self.selection('INBOX',[restored]),[restored],'trash')
        self.assertGreater(TrashEntry.objects.get().trashed_at,timezone.now()-timedelta(minutes=1))

    def test_no_unsafe_expunge_when_uidplus_missing(self):
        self.server.capabilities=(b'MOVE',)
        trash.apply_selection(self.worker,self.mailbox.pk,self.selection(),['1'],'trash')
        entry=TrashEntry.objects.get()
        with self.assertRaises(ValidationError):trash.cleanup_box(self.mailbox.pk,entry.trashed_at+timedelta(days=31))
        self.assertFalse(any(command=='EXPUNGE' for _,command,_ in self.server.commands))
        self.assertTrue(self.server.data['Trash'])

    def test_copy_fallback_expunge_is_scoped_to_selected_source_uids(self):
        self.server.capabilities=(b'UIDPLUS',)
        trash.apply_selection(self.worker,self.mailbox.pk,self.selection(),['1'],'trash')
        self.assertEqual(set(self.server.data['INBOX']),{2,3});self.assertEqual(len(self.server.data['Trash']),1)
        self.assertIn(('INBOX','EXPUNGE',('1',)),self.server.commands)

    def test_purge_removes_cached_sent_content_but_keeps_delivery_audit(self):
        from .models import Letter,Attachment
        letter=Letter.objects.create(mailbox=self.mailbox,author=self.worker,state='sent',to='receiver@example.org',body='PRIVATE',subject='PRIVATE',message_id='<test-cache@armada-sibir.ru>')
        Attachment.objects.create(letter=letter,name='private.txt',data=b'PRIVATE')
        self.server.data['INBOX'][1]=b'Message-ID: <test-cache@armada-sibir.ru>\r\n'+RAW
        trash.apply_selection(self.worker,self.mailbox.pk,self.selection(),['1'],'trash')
        entry=TrashEntry.objects.get();trash.cleanup_box(self.mailbox.pk,entry.trashed_at+timedelta(days=31))
        letter.refresh_from_db();self.assertEqual(letter.state,'sent');self.assertEqual(letter.body,'')
        self.assertIsNotNone(letter.purged_at);self.assertFalse(letter.attachments.exists())
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse('mail_local_letter',args=[self.mailbox.pk,letter.pk])).status_code,404)

    def test_duplicate_message_id_does_not_erase_existing_sent_copy(self):
        from .models import Letter
        letter=Letter.objects.create(mailbox=self.mailbox,author=self.worker,state='sent',body='PRIVATE',message_id='<duplicate@armada-sibir.ru>')
        raw=b'Message-ID: <duplicate@armada-sibir.ru>\r\n'+RAW
        self.server.data['Sent'][55]=raw;self.server.data['INBOX'][1]=raw
        trash.apply_selection(self.worker,self.mailbox.pk,self.selection(),['1'],'trash')
        entry=TrashEntry.objects.get();trash.cleanup_box(self.mailbox.pk,entry.trashed_at+timedelta(days=31))
        letter.refresh_from_db();self.assertIsNone(letter.purged_at);self.assertEqual(letter.body,'PRIVATE')

    def test_shared_sender_can_queue_but_revoked_access_stops_delivery(self):
        box=services.configure(self.leader,self.mailbox.pk,self.mailbox.version,'',None,None,True,members=[self.worker,self.other])
        letter=services.save_letter(self.other,box.pk,uuid.uuid4(),0,dict(to='test@example.org',subject='ТЕСТ',body='ТЕСТ',cc='',bcc=''),[],queue=True)
        services.configure(self.leader,box.pk,box.version,'',None,None,True,members=[self.worker])
        with patch('postoffice.transport.deliver') as deliver:
            self.assertTrue(services.process_one());deliver.assert_not_called()
        letter.refresh_from_db();self.assertEqual(letter.state,'failed')

    def test_stale_version_and_invalid_selection_cannot_move_messages(self):
        selection=self.selection()
        services.configure(self.leader,self.mailbox.pk,self.mailbox.version,'',None,None,True,members=[self.worker,self.other])
        with self.assertRaises(PermissionDenied):trash.apply_selection(self.worker,self.mailbox.pk,selection,['1'],'trash')
        self.mailbox.refresh_from_db()
        for selection,uids in [(self.selection()+'tampered',['1']),(self.selection(),[]),(self.selection('INBOX',['4294967296']),['4294967296'])]:
            with self.assertRaises(ValidationError):trash.apply_selection(self.worker,self.mailbox.pk,selection,uids,'trash')
        self.assertEqual(len(self.server.data['INBOX']),3)


    def test_strict_status_reports_missing_capabilities_without_mutation(self):
        import io
        from django.core.management import call_command,CommandError
        self.server.capabilities=('IMAP4REV1',)
        output=io.StringIO()
        with self.assertRaises(CommandError):call_command('mail_trash_status',require_trash=True,stdout=output)
        self.assertIn('перемещение недоступно',output.getvalue())
        self.assertEqual(self.server.commands,[])
        self.assertFalse(TrashEntry.objects.exists())
