import socketserver
from datetime import timedelta
from django.test import TestCase,override_settings
from cryptography.fernet import Fernet
from . import tests as fixture,test_transport as tls,test_shared_trash as shared,trash
from .models import TrashEntry


class RetentionIMAP(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5);self.wfile.write(b'* OK IMAP ready\r\n')
        backend=self.server.backend
        while line:=self.rfile.readline():
            tag,cmd=line.split(b' ',1);parts=cmd.decode().strip().split();name=parts[0].upper()
            if name=='CAPABILITY':self.wfile.write(b'* CAPABILITY IMAP4rev1 UIDPLUS MOVE\r\n')
            elif name=='LOGIN':pass
            elif name=='LIST':
                for row in backend.list()[1]:self.wfile.write(b'* LIST '+row+b'\r\n')
            elif name in ('SELECT','EXAMINE'):
                backend.select(parts[1]);self.wfile.write(b'* 3 EXISTS\r\n* OK [UIDVALIDITY '+str(backend.generations[backend.folder]).encode()+b'] ready\r\n')
            elif name=='UID':
                command=parts[1].upper()
                args=parts[2:]
                if command=='SEARCH':args=[None,*args]
                status,data=backend.uid(command,*args)
                if command=='SEARCH':self.wfile.write(b'* SEARCH '+data[0]+b'\r\n')
                elif command=='FETCH':
                    for row in data:
                        if isinstance(row,tuple):self.wfile.write(b'* '+row[0].replace(b'1 (',b'1 FETCH (',1)+b'\r\n'+row[1]+b')\r\n')
            elif name=='LOGOUT':
                self.wfile.write(b'* BYE\r\n'+tag+b' OK logout\r\n');break
            self.wfile.write(tag+b' OK done\r\n')


@override_settings(ARMADA_MAIL_KEY=Fernet.generate_key().decode())
class TrashTLSTests(TestCase):
    setUpTestData=classmethod(fixture.MailTests.setUpTestData.__func__)
    box=fixture.MailTests.box
    server=tls.TransportTLSTests.server
    def test_real_tls_imap_move_and_due_uid_expunge(self):
        box=self.box()
        with self.server(RetentionIMAP) as srv,override_settings(ARMADA_IMAP_HOST='localhost',ARMADA_IMAP_PORT=srv.server_address[1]):
            srv.backend=shared.MemoryIMAP()
            selection=trash.token(self.worker,box,'INBOX','7',['1','2'])
            trash.apply_selection(self.worker,box.pk,selection,['1','2'],'trash')
            self.assertEqual(set(srv.backend.data['INBOX']),{3});self.assertEqual(TrashEntry.objects.count(),2)
            start=TrashEntry.objects.order_by('trashed_at').last().trashed_at
            srv.backend.data['Trash'][999]=shared.RAW;srv.backend.deleted['Trash']={999}
            self.assertEqual(trash.cleanup_box(box.pk,start+timedelta(days=30,seconds=1)),2)
            self.assertEqual(set(srv.backend.data['Trash']),{999})
