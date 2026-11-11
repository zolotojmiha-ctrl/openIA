"""Real local TLS handshakes; no MAX network or production credentials."""
import ipaddress,json,ssl,tempfile,threading
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import urlopen
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.test import SimpleTestCase,override_settings
from . import transport

@override_settings(ARMADA_MAX_TOKEN='SYNTHETIC_TLS_TEST_TOKEN')
class APITLSTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass();cls.temp=tempfile.TemporaryDirectory(prefix='armada-max-tls-');root=Path(cls.temp.name)
        now=datetime.now(timezone.utc);ca_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Synthetic Armada Test CA')])
        ca=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).sign(ca_key,hashes.SHA256()))
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cert=(x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False).sign(ca_key,hashes.SHA256()))
        cls.ca=root/'ca.pem';cls.ca.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        certfile=root/'server.pem';certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        keyfile=root/'key.pem';keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));keyfile.chmod(0o600)
        cls.events=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                cls.events.append((self.path,self.headers.get('Authorization')))
                if self.path=='/redirect':
                    self.send_response(302);self.send_header('Location',cls.origin+'/target');self.end_headers();return
                body=json.dumps({'is_bot':True,'username':'synthetic_tls_bot'}).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(certfile,keyfile)
        cls.server.socket=context.wrap_socket(cls.server.socket,server_side=True)
        cls.origin='https://127.0.0.1:'+str(cls.server.server_port)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=5);cls.temp.cleanup();super().tearDownClass()
    def test_ca_is_scoped_to_api_and_system_trust_stays_unchanged(self):
        with patch.object(transport,'API',self.origin),patch.object(transport,'API_CA_FILE',self.ca):
            result=transport.api('GET','/me')
            self.assertEqual(result['username'],'synthetic_tls_bot')
            context=transport.api_ssl_context()
            self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED);self.assertTrue(context.check_hostname)
        self.assertEqual(self.events[-1],('/me','SYNTHETIC_TLS_TEST_TOKEN'))
        with self.assertRaises(URLError):urlopen(self.origin+'/outside',timeout=5,context=ssl.create_default_context())
    def test_untrusted_api_is_rejected_without_operator_ca(self):
        with patch.object(transport,'API',self.origin),patch.object(transport,'API_CA_FILE',Path(self.temp.name)/'missing.pem'),self.assertRaises(transport.DeliveryError):transport.api('GET','/me')
    def test_redirect_is_not_followed_even_with_operator_ca(self):
        before=len(self.events)
        with patch.object(transport,'API',self.origin),patch.object(transport,'API_CA_FILE',self.ca),self.assertRaises(transport.DeliveryError):transport.api('GET','/redirect')
        self.assertEqual(self.events[before:],[('/redirect','SYNTHETIC_TLS_TEST_TOKEN')])
    def test_invalid_ca_fails_closed_before_sending_token(self):
        invalid=Path(self.temp.name)/'invalid.pem';invalid.write_text('SYNTHETIC INVALID CERTIFICATE')
        before=len(self.events)
        with patch.object(transport,'API',self.origin),patch.object(transport,'API_CA_FILE',invalid),self.assertRaises(transport.DeliveryError):transport.api('GET','/me')
        self.assertEqual(len(self.events),before)
