"""Verify the host backup freezes the opt-in worker and resumes only prior writers."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from django.conf import settings
from django.test import SimpleTestCase


class WriterBackupTests(SimpleTestCase):
    def run_backup(self, writers, fail=False, hold=False, legacy=False):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for name in ('backup.sh', 'compose.yml', 'Caddyfile','backup_counts.sql','backup_manifest.py','backup_retention.py'):
                shutil.copy(settings.BASE_DIR / 'deploy/beget' / name, root / name)
            docker = root / 'docker'
            docker.write_text('''#!/usr/bin/env python3
import os, sys
a=sys.argv[1:]
with open(os.environ['MAX_TEST_LOG'],'a') as f:f.write(' '.join(a)+'\\n')
if 'config' in a: print('web\\nmailworker'+('' if os.environ.get('MAX_TEST_LEGACY')=='1' else '\\nmaxworker'))
elif 'ps' in a and 'maxworker' in a and os.environ.get('MAX_TEST_LEGACY')=='1':sys.exit(91)
elif 'ps' in a: print(os.environ['MAX_TEST_WRITERS'])
elif 'pg_dump' in a:
 if os.environ.get('MAX_TEST_FAIL')=='1':sys.exit(42)
 sys.stdout.buffer.write(b'SYNTHETIC DUMP')
elif 'psql' in a: print('{}')
elif 'pg_restore' in a: print('SYNTHETIC CONTENTS')
elif 'tar' in a:
 import io,tarfile
 b=io.BytesIO()
 with tarfile.open(fileobj=b,mode='w'):pass
 sys.stdout.buffer.write(b.getvalue())
''')
            docker.chmod(0o700)
            log = root / 'calls'
            env = os.environ | {'PATH': str(root) + ':' + os.environ['PATH'], 'ARMADA_BACKUP_DIR': str(root / 'backups'),
                                'MAX_TEST_LOG': str(log), 'MAX_TEST_WRITERS': '\n'.join(writers), 'MAX_TEST_FAIL': '1' if fail else '0','MAX_TEST_LEGACY':'1' if legacy else '0'}
            args=['bash', str(root / 'backup.sh')]
            if hold:args+=['--hold-writers','--state-file',str(root/'writers')]
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 42 if fail else 0, result.stderr)
            lines = log.read_text().splitlines()
            stop = [line.split(' stop ')[1] for line in lines if ' stop ' in line]
            start = [line.split(' start ')[1] for line in lines if ' start ' in line]
            self.assertEqual(stop, [' '.join(writers)] if writers else [])
            self.assertEqual(start, [] if hold else stop)
            if hold:self.assertEqual((root/'writers').read_text().splitlines(),writers)

    def test_backup_stops_max_and_resumes_it_after_success(self):
        self.run_backup(['web', 'mailworker', 'maxworker'])

    def test_disabled_max_is_not_started_by_backup(self):
        self.run_backup(['web', 'mailworker'])

    def test_backup_failure_still_resumes_original_writers(self):
        self.run_backup(['web', 'maxworker'], fail=True)

    def test_held_backup_does_not_reopen_write_window(self):
        self.run_backup(['web', 'mailworker', 'maxworker'], hold=True)

    def test_held_backup_failure_leaves_writers_stopped_for_installer(self):
        self.run_backup(['web', 'mailworker'], fail=True, hold=True)

    def test_legacy_compose_without_max_service_is_supported(self):
        self.run_backup(['web', 'mailworker'], legacy=True)
