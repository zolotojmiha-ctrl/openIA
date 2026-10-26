import socketserver,ssl,tempfile,threading,ipaddress,base64
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from contextlib import contextmanager
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.fernet import Fernet
from django.test import SimpleTestCase,override_settings
from email.message import EmailMessage
from .crypto import seal
from . import transport

RAW=b'From: sender@example.org\r\nTo: worker@armada-sibir.ru\r\nSubject: TLS message\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHello over TLS'
class IMAP(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5);self.wfile.write(b'* OK IMAP ready\r\n')
        seen=False;writable=False
        while line:=self.rfile.readline():
            tag,cmd=line.split(b' ',1);upper=cmd.upper()
            if upper.startswith(b'CAPABILITY'):self.wfile.write(b'* CAPABILITY IMAP4rev1 UIDPLUS MOVE\r\n')
            elif upper.startswith(b'LIST'):self.wfile.write(b'* LIST (\\HasNoChildren) "/" "INBOX"\r\n* LIST (\\Sent) "/" "Sent"\r\n')
            elif upper.startswith((b'SELECT',b'EXAMINE')):
                writable=upper.startswith(b'SELECT');self.wfile.write(b'* 1 EXISTS\r\n* OK [UIDVALIDITY 7] ready\r\n')
            elif upper.startswith(b'UID STORE'):
                if not writable or cmd.strip()!=b'UID STORE 1 +FLAGS (\\Seen)':
                    self.wfile.write(tag+b' NO bad flag or readonly\r\n');continue
                seen=True;self.wfile.write(b'* 1 FETCH (UID 1 FLAGS (\\Seen))\r\n')
            elif upper.startswith(b'UID SEARCH'):self.wfile.write(b'* SEARCH'+(b'' if seen and b'UNSEEN' in upper else b' 1')+b'\r\n')
            elif b'RFC822.SIZE' in upper:self.wfile.write(b'* 1 FETCH (UID 1 RFC822.SIZE '+str(len(RAW)).encode()+b')\r\n')
            elif upper.startswith(b'UID FETCH'):
                body=RAW.split(b'\r\n\r\n')[0]+b'\r\n\r\n' if b'HEADER.FIELDS' in upper else RAW
                self.wfile.write(b'* 1 FETCH (UID 1 BODY[] {'+str(len(body)).encode()+b'}\r\n'+body+b')\r\n')
            elif upper.startswith(b'LOGOUT'):
                self.wfile.write(b'* BYE\r\n'+tag+b' OK logout\r\n');break
            self.wfile.write(tag+b' OK done\r\n')
class SMTP(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5);self.wfile.write(b'220 localhost ESMTP\r\n')
        while line:=self.rfile.readline():
            upper=line.upper()
            if upper.startswith(b'EHLO'):self.wfile.write(b'250-localhost\r\n250 AUTH PLAIN\r\n')
            elif upper.startswith(b'AUTH PLAIN '):
                self.server.auth=base64.b64decode(line.strip().split()[-1]);self.wfile.write(b'235 authenticated\r\n')
            elif upper.startswith(b'DATA'):
                self.wfile.write(b'354 data\r\n');data=[]
                while (part:=self.rfile.readline()) and part!=b'.\r\n':data.append(part)
                self.server.message=b''.join(data);self.wfile.write(b'250 accepted\r\n')
            elif upper.startswith(b'QUIT'):self.wfile.write(b'221 bye\r\n');break
            else:self.wfile.write(b'250 ok\r\n')
class TLS(socketserver.ThreadingTCPServer):
    allow_reuse_address=True
    daemon_threads=True
    def get_request(self):
        sock,address=super().get_request();return self.context.wrap_socket(sock,server_side=True),address

@override_settings(ARMADA_MAIL_KEY=Fernet.generate_key().decode())
class TransportTLSTests(SimpleTestCase):
    @contextmanager
    def server(self,handler):
        with tempfile.TemporaryDirectory() as tmp:
            key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
            name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')]);now=datetime.now(timezone.utc)
            cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False).sign(key,hashes.SHA256())
            root=Path(tmp);(root/'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM));(root/'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(root/'cert.pem',root/'key.pem')
            client_context=ssl.create_default_context(cafile=str(root/'cert.pem'))
            server=TLS(('127.0.0.1',0),handler);server.context=context
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with patch('postoffice.transport.ssl.create_default_context',return_value=client_context):yield server
            finally:server.shutdown();server.server_close();thread.join(timeout=5)
    def box(self):return SimpleNamespace(state='active',address='worker@armada-sibir.ru',credential=seal('synthetic-provider-secret'))
    def test_actual_imap_tls_login_list_fetch_and_safe_body(self):
        with self.server(IMAP) as srv,override_settings(ARMADA_IMAP_HOST='localhost',ARMADA_IMAP_PORT=srv.server_address[1]):
            with transport.connect(self.box()) as client:
                self.assertEqual(transport.folders(client)[0]['name'],'INBOX')
                rows,total=transport.listing(client,'INBOX');self.assertEqual(total,1);self.assertEqual(rows[0]['subject'],'TLS message')
                message=transport.message(client,'INBOX',1);self.assertEqual(message['body'],'Hello over TLS')
    def test_actual_smtp_tls_auth_and_message_submission(self):
        with self.server(SMTP) as srv,override_settings(ARMADA_SMTP_HOST='localhost',ARMADA_SMTP_PORT=srv.server_address[1],ARMADA_SMTP_STARTTLS=False):
            msg=EmailMessage();msg['From']='worker@armada-sibir.ru';msg['To']='receiver@example.org';msg['Subject']='TLS message';msg.set_content('Hello')
            self.assertEqual(transport.deliver(self.box(),msg,['receiver@example.org']),{})
            self.assertIn(b'Subject: TLS message',srv.message);self.assertEqual(srv.auth,b'\0worker@armada-sibir.ru\0synthetic-provider-secret')

    def test_actual_imap_tls_read_flag_and_unread_filter(self):
        with self.server(IMAP) as srv,override_settings(ARMADA_IMAP_HOST='localhost',ARMADA_IMAP_PORT=srv.server_address[1]):
            with transport.connect(self.box()) as client:
                rows,total=transport.listing(client,'INBOX',unread_only=True);self.assertEqual(total,1)
                transport.select(client,'INBOX',readonly=False)
                transport.checked(client.uid('STORE','1','+FLAGS','(\\Seen)'))
                rows,total=transport.listing(client,'INBOX',unread_only=True);self.assertEqual(total,0)
