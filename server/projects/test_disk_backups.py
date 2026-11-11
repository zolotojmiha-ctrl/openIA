import hashlib,io,os,tempfile,uuid
from datetime import datetime,time,timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from unittest.mock import patch,Mock
from django.test import TestCase,SimpleTestCase
from django.urls import reverse
from django.core.exceptions import ValidationError,PermissionDenied
from django.utils import timezone
from . import disk_crypto,disk_backups
from .yandex_disk import Client,DiskError,signed_url,Redirects
from .models import DiskBackupSettings,DiskBackupJob
from . import test_stock as fixture

TOKEN='y0_SYNTHETIC_NOT_A_REAL_TOKEN_123456789'

class EncryptionTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass();cls.private,cls.public,cls.fingerprint=disk_crypto.generate()
    def test_stream_roundtrip_of_large_binary_archive(self):
        with tempfile.TemporaryDirectory() as root:
            src=Path(root)/'source';enc=Path(root)/'encrypted';dst=Path(root)/'restored'
            raw=os.urandom(disk_crypto.CHUNK*2+73);src.write_bytes(raw)
            sha,size=disk_crypto.encrypt(src,enc,self.public)
            self.assertEqual((sha,size),disk_crypto.digest(enc))
            self.assertNotIn(raw[:100],enc.read_bytes())
            disk_crypto.decrypt(enc,dst,self.private)
            self.assertEqual(dst.read_bytes(),raw)
    def test_tampered_archive_never_produces_a_recovery_file(self):
        with tempfile.TemporaryDirectory() as root:
            src=Path(root)/'s';enc=Path(root)/'e';dst=Path(root)/'d';src.write_bytes(b'private database and documents')
            disk_crypto.encrypt(src,enc,self.public)
            data=bytearray(enc.read_bytes());data[-20]^=1;enc.write_bytes(data)
            with self.assertRaises(Exception):disk_crypto.decrypt(enc,dst,self.private)
            self.assertFalse(dst.exists());self.assertFalse(dst.with_name('d.partial').exists())
    def test_existing_destination_is_never_overwritten_or_deleted(self):
        with tempfile.TemporaryDirectory() as root:
            src=Path(root)/'s';enc=Path(root)/'e';src.write_bytes(b'plain');enc.write_bytes(b'preserve')
            with self.assertRaises(FileExistsError):disk_crypto.encrypt(src,enc,self.public)
            self.assertEqual(enc.read_bytes(),b'preserve')
            with self.assertRaises(ValueError):disk_crypto.decrypt(src,enc,self.private)
            self.assertEqual(enc.read_bytes(),b'preserve')
    def test_wrong_key_does_not_publish_partial_recovery(self):
        other,_,_=disk_crypto.generate()
        with tempfile.TemporaryDirectory() as root:
            src=Path(root)/'s';enc=Path(root)/'e';dst=Path(root)/'d';src.write_bytes(b'test')
            disk_crypto.encrypt(src,enc,self.public)
            with self.assertRaises(Exception):disk_crypto.decrypt(enc,dst,other)
            self.assertFalse(dst.exists())

class APIContractTests(SimpleTestCase):
    def test_signed_hosts_are_restricted_and_never_use_http(self):
        for value in ['http://uploader.disk.yandex.net/x','https://evil.example/x','https://uploader.disk.yandex.net.evil.example/x','https://user@uploader.disk.yandex.net/x','https://127.0.0.1/x']:
            with self.assertRaises(DiskError):signed_url(value)
        self.assertEqual(signed_url('https://uploader.disk.yandex.net/x'),'https://uploader.disk.yandex.net/x')
    def test_api_does_not_forward_oauth_on_redirect(self):
        from urllib.request import Request
        req=Request('https://cloud-api.yandex.net/v1/disk',headers={'Authorization':'OAuth '+TOKEN})
        with self.assertRaises(DiskError):Redirects().redirect_request(req,None,302,'',{},'https://uploader.disk.yandex.net/x')
    def test_api_uses_header_not_query_and_only_requests_quota_fields(self):
        requests=[]
        class Response(io.BytesIO):
            status=200
        class Opener:
            def open(self,request,timeout):
                requests.append(request)
                return Response(b'{"total_space":2000000000000,"used_space":20}' if len(requests)==1 else b'{"type":"dir"}')
        total,used=Client(TOKEN,Opener()).info()
        self.assertEqual(total,2000000000000);self.assertEqual(used,20)
        self.assertTrue(all(TOKEN not in r.full_url for r in requests))
        self.assertTrue(all(r.get_header('Authorization')=='OAuth '+TOKEN for r in requests))
    def test_storage_upload_and_download_have_no_oauth_header(self):
        requests=[];raw=b'encrypted test payload'
        class Response(io.BytesIO):status=201
        class Opener:
            def open(self,request,timeout):
                requests.append(request)
                if 'cloud-api.yandex.net' in request.full_url:
                    return Response(b'{"href":"https://uploader.disk.yandex.net/x","method":"PUT"}' if 'upload?' in request.full_url else b'{"href":"https://downloader.disk.yandex.ru/x","method":"GET"}')
                if request.method=='PUT':
                    self.uploaded=request.data.read();return Response(b'')
                return Response(raw)
        opener=Opener();client=Client(TOKEN,opener)
        with tempfile.TemporaryDirectory() as root:
            src=Path(root)/'s';src.write_bytes(raw);client.upload('app:/armada/backups/test',src)
            client.verify('app:/armada/backups/test',hashlib.sha256(raw).hexdigest(),len(raw))
        self.assertEqual(opener.uploaded,raw)
        for r in requests:
            if 'cloud-api.yandex.net' not in r.full_url:self.assertFalse(r.has_header('Authorization'))
    def test_hash_mismatch_is_not_success(self):
        class Response(io.BytesIO):status=200
        class Opener:
            def open(self,request,timeout):
                return Response(b'{"href":"https://downloader.disk.yandex.ru/x"}' if 'cloud-api.yandex.net' in request.full_url else b'corrupt')
        with self.assertRaises(DiskError):Client(TOKEN,Opener()).verify('app:/armada/backups/test','0'*64,7)

class DiskBackupTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    def setUp(self):self.client.force_login(self.leader)
    def configure(self):
        config=disk_backups.save(self.leader,1,TOKEN,False,time(3))
        with patch('projects.disk_backups.Client.info',return_value=(2*10**12,0)):disk_backups.check(self.leader,config.version)
        self.private,self.fingerprint=disk_backups.create_key(self.leader,config.version)
        disk_backups.confirm_key(self.leader,self.fingerprint)
        return DiskBackupSettings.objects.get(pk=1)
    def test_token_is_encrypted_and_never_echoed_in_form(self):
        config=self.configure()
        self.assertNotEqual(config.credential,TOKEN);self.assertNotIn(TOKEN,config.credential)
        response=self.client.get(reverse('disk_backup_settings'))
        self.assertEqual(response.status_code,200);self.assertNotContains(response,TOKEN)
        self.assertNotIn(self.private.decode(),response.content.decode())
        self.assertFalse(config.enabled)
    def test_only_leaders_can_access_setup(self):
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse('disk_backup_settings')).status_code,403)
        self.assertEqual(self.client.post(reverse('disk_backup_settings'),{'action':'key','version':'1'}).status_code,403)
    def test_manual_job_requires_confirmed_key_and_is_idempotent(self):
        config=disk_backups.save(self.leader,1,TOKEN,False,time(3))
        with self.assertRaises(ValidationError):disk_backups.enqueue(self.leader,uuid.uuid4())
        self.configure_existing(config)
        token=uuid.uuid4();first=disk_backups.enqueue(self.leader,token)
        self.assertEqual(disk_backups.enqueue(self.leader,token).pk,first.pk)
        with self.assertRaises(ValidationError):disk_backups.enqueue(self.leader,uuid.uuid4())
    def configure_existing(self,config):
        with patch('projects.disk_backups.Client.info',return_value=(2*10**12,0)):disk_backups.check(self.leader,config.version)
        self.private,self.fingerprint=disk_backups.create_key(self.leader,config.version)
        disk_backups.confirm_key(self.leader,self.fingerprint)
    def test_error_actions_render_a_message_instead_of_exposing_secrets(self):
        response=self.client.post(reverse('disk_backup_settings'),{'action':'backup','token_id':str(uuid.uuid4())})
        # Missing settings are handled without a traceback.
        self.assertIn(response.status_code,(200,400))
        self.assertNotContains(response,TOKEN)
    def test_key_download_is_private_and_not_stored_in_database(self):
        self.configure()
        config=DiskBackupSettings.objects.get(pk=1)
        self.assertNotIn('PRIVATE KEY',config.public_key)
        self.assertIn('PUBLIC KEY',config.public_key)
        self.assertFalse(any('PRIVATE KEY' in str(v) for v in vars(config).values()))
    def test_schedule_requires_worker_and_does_not_enable_itself(self):
        config=self.configure()
        with self.assertRaises(ValidationError):disk_backups.save(self.leader,config.version,'',True,time(3))
        DiskBackupSettings.objects.filter(pk=1).update(worker_seen_at=timezone.now())
        disk_backups.save(self.leader,config.version,'',True,time(3))
        config.refresh_from_db();self.assertTrue(config.enabled)
    def test_changed_settings_cancel_waiting_jobs(self):
        config=self.configure();job=disk_backups.enqueue(self.leader,uuid.uuid4())
        disk_backups.save(self.leader,config.version,'',False,time(4))
        self.assertIsNone(disk_backups.claim())
        job.refresh_from_db();self.assertEqual(job.state,'failed')
    def test_upload_is_verified_by_full_readback_and_archive_is_retained(self):
        self.configure();job=disk_backups.enqueue(self.leader,uuid.uuid4());disk_backups.claim()
        remote={}
        class Fake:
            def __init__(self,token):pass
            def info(self):return 2*10**12,0
            def mkdirs(self):pass
            def metadata(self,path):raise FileNotFoundError
            def upload(self,path,source):remote[path]=Path(source).read_bytes()
            def verify(self,path,sha,size):
                if len(remote[path])!=size or hashlib.sha256(remote[path]).hexdigest()!=sha:raise DiskError('bad')
        with tempfile.TemporaryDirectory() as root:
            source=Path(root)/'archive';source.write_bytes(b'fake database and immutable files')
            with patch('projects.disk_backups.Client',Fake):disk_backups.upload(job.pk,source,Path(root))
            self.assertEqual(source.read_bytes(),b'fake database and immutable files')
            job.refresh_from_db();self.assertEqual(job.state,'verified')
            encrypted=Path(root)/'downloaded';encrypted.write_bytes(remote[job.remote_path])
            restored=Path(root)/'restored';disk_crypto.decrypt(encrypted,restored,self.private)
            self.assertEqual(restored.read_bytes(),source.read_bytes())
    def test_preencrypted_local_copy_is_uploaded_unchanged(self):
        self.configure();job=disk_backups.enqueue(self.leader,uuid.uuid4());disk_backups.claim()
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);plain=root/'plain';plain.write_bytes(b'SYNTHETIC');source=root/'copy.encrypted';disk_crypto.encrypt(plain,source,job.public_key)
            raw=source.read_bytes()
            with patch('projects.disk_backups.Client') as client:
                client.return_value.info.return_value=(2*10**12,0)
                client.return_value.metadata.side_effect=FileNotFoundError
                disk_backups.upload(job.pk,source,root,encrypted_key_id=job.key_fingerprint)
                client.return_value.upload.assert_called_once()
                self.assertEqual(client.return_value.upload.call_args.args[1],source)
            self.assertEqual(source.read_bytes(),raw);job.refresh_from_db();self.assertEqual(job.state,'verified')

    def test_preencrypted_copy_with_another_key_is_retained_and_not_sent(self):
        self.configure();job=disk_backups.enqueue(self.leader,uuid.uuid4());disk_backups.claim()
        with tempfile.TemporaryDirectory() as root:
            source=Path(root)/'copy';source.write_bytes(b'SYNTHETIC')
            with patch('projects.disk_backups.Client') as client:
                with self.assertRaises(ValidationError):disk_backups.upload(job.pk,source,Path(root),encrypted_key_id='wrong-public-fingerprint')
                client.assert_not_called()
            self.assertTrue(source.exists());job.refresh_from_db();self.assertEqual(job.state,'failed')

    def test_upload_failure_never_claims_success_or_removes_original(self):
        self.configure();job=disk_backups.enqueue(self.leader,uuid.uuid4());disk_backups.claim()
        with tempfile.TemporaryDirectory() as root:
            source=Path(root)/'archive';source.write_bytes(b'preserve')
            with patch('projects.disk_backups.Client') as client:
                client.return_value.info.return_value=(2*10**12,0)
                client.return_value.metadata.side_effect=FileNotFoundError
                client.return_value.verify.side_effect=DiskError('tampered')
                with self.assertRaises(DiskError):disk_backups.upload(job.pk,source,Path(root))
            job.refresh_from_db();self.assertEqual(job.state,'failed');self.assertTrue(source.exists())

    def test_key_download_response_and_invalid_token_form_are_not_cached_or_echoed(self):
        url=reverse('disk_backup_settings')
        response=self.client.post(url,{'action':'key','version':1})
        self.assertEqual(response.status_code,200)
        self.assertIn('PRIVATE KEY',response.content.decode())
        self.assertIn('no-store',response['Cache-Control'])
        self.assertIn('attachment;',response['Content-Disposition'])
        response=self.client.post(url,{'action':'save','version':1,'token':TOKEN,'backup_time':'bad'})
        self.assertEqual(response.status_code,200);self.assertNotContains(response,TOKEN)

    def test_daily_schedule_uses_omsk_once_per_day_without_catching_up_missed_days(self):
        self.configure()
        now=datetime(2026,11,4,4,tzinfo=ZoneInfo('Asia/Omsk'))
        DiskBackupSettings.objects.filter(pk=1).update(enabled=True,not_before=now-timedelta(days=8))
        first=disk_backups.claim(now)
        self.assertIsNotNone(first)
        self.assertEqual(DiskBackupJob.objects.get(pk=first).automatic_day,now.date())
        DiskBackupJob.objects.filter(pk=first).update(state='verified')
        self.assertIsNone(disk_backups.claim(now+timedelta(hours=1)))
        second=disk_backups.claim(now+timedelta(days=1))
        self.assertIsNotNone(second)
        self.assertEqual(DiskBackupJob.objects.count(),2)

    def test_schedule_enabled_after_due_time_waits_until_next_day(self):
        self.configure()
        now=datetime(2026,11,4,4,tzinfo=ZoneInfo('Asia/Omsk'))
        DiskBackupSettings.objects.filter(pk=1).update(enabled=True,not_before=now)
        self.assertIsNone(disk_backups.claim(now))
        self.assertIsNotNone(disk_backups.claim(now+timedelta(days=1)))

    def test_revoked_author_cannot_start_queued_job(self):
        self.configure();job=disk_backups.enqueue(self.leader,uuid.uuid4())
        type(self.leader).objects.filter(pk=self.leader.pk).update(is_active=False)
        self.assertIsNone(disk_backups.claim())
        job.refresh_from_db();self.assertEqual(job.state,'failed')

class HostWorkerTests(SimpleTestCase):
    def run_worker(self,stage=''):
        import json,shutil,subprocess
        root_source=Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);binpath=root/'bin';binpath.mkdir();backup=root/'backups'
            for name in ('disk-backups.sh','backup.sh','backup_counts.sql','backup_manifest.py','backup_retention.py'):
                shutil.copy2(root_source/'deploy/beget'/name,root/name)
            (root/'compose.yml').write_text('synthetic test compose')
            (root/'Caddyfile').write_text('synthetic test config')
            docker=binpath/'docker'
            docker.write_text('''#!/usr/bin/env python3
import os,sys,json
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['TEST_LOG'],'a') as log:log.write(json.dumps(args)+'\\n')
if args[-1]=='claim':print('12345678-1234-1234-1234-123456789abc')
elif 'config' in args:print('web\\nmailworker\\nmaxworker')
elif 'ps' in args:print(chr(10).join(['web','mailworker','maxworker']))
elif 'pg_dump' in args:
    if os.environ['FAIL_STAGE']=='capture':sys.exit(1)
    sys.stdout.buffer.write(b'synthetic dump')
elif 'psql' in args:print('{}')
elif 'upload' in args and os.environ['FAIL_STAGE']=='upload':sys.exit(1)
elif 'tar' in args:
    import io,tarfile
    b=io.BytesIO()
    with tarfile.open(fileobj=b,mode='w'):pass
    sys.stdout.buffer.write(b.getvalue())
''');docker.chmod(0o755)
            env=os.environ|{'PATH':str(binpath)+os.pathsep+os.environ['PATH'],'TEST_LOG':str(root/'calls'),'FAIL_STAGE':stage,'ARMADA_BACKUP_DIR':str(backup)+'/'}
            result=subprocess.run(['bash',str(root/'disk-backups.sh')],env=env,capture_output=True,text=True,timeout=15)
            calls=[json.loads(line) for line in (root/'calls').read_text().splitlines()]
            archives=list(backup.glob('armada-*.tar.gz'))
            self.assertFalse(list(backup.glob('.disk-work.*')))
            self.assertFalse(any('docker.sock' in arg for call in calls for arg in call))
            return result,calls,len(archives)
    def test_host_captures_consistent_local_archive_before_upload_and_retains_it(self):
        result,calls,count=self.run_worker()
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(count,1)
        self.assertLess(next(i for i,c in enumerate(calls) if 'pg_dump' in c),next(i for i,c in enumerate(calls) if 'upload' in c))
        upload=next(c for c in calls if 'upload' in c)
        self.assertEqual(upload[upload.index('--cap-add')+1],'DAC_OVERRIDE')
        self.assertEqual(upload[upload.index('--user')+1],'0:0')
        self.assertNotIn('--privileged',upload)
        self.assertFalse(any('--cap-add' in c for c in calls if 'exec' in c))
        self.assertTrue(any('start' in c for c in calls))
        self.assertEqual(next(c[-3:] for c in calls if 'stop' in c),['web','mailworker','maxworker'])
        self.assertEqual(next(c[-3:] for c in calls if 'start' in c),['web','mailworker','maxworker'])
    def test_capture_failure_restarts_services_and_never_uploads(self):
        result,calls,count=self.run_worker('capture')
        self.assertNotEqual(result.returncode,0);self.assertEqual(count,0)
        self.assertFalse(any('upload' in c for c in calls));self.assertTrue(any('start' in c for c in calls));self.assertTrue(any('fail' in c for c in calls))
    def test_upload_failure_preserves_local_archive_and_marks_job_failed(self):
        result,calls,count=self.run_worker('upload')
        self.assertNotEqual(result.returncode,0);self.assertEqual(count,1)
        self.assertTrue(any('fail' in c for c in calls))
