import contextlib
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_policy', ROOT / 'scripts/release_6331.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ReleasePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='armada-policy-tests-')
        cls.root = Path(cls.temp.name)
        key = cls.root / 'synthetic-signing.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', str(key)], check=True, capture_output=True)
        cls.key_text = key.read_text()
        cls.fp = hashlib.sha256(m.openssl('pkey', '-in', key, '-pubout', '-outform', 'DER')).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix='case-', dir=self.root)
        self.addCleanup(self.work.cleanup)
        self.root_case = Path(self.work.name)
        self.input = self.root_case / 'unsigned'
        self.input.mkdir()
        (self.input / m.ARCHIVE).write_bytes(b'SYNTHETIC ZIP')
        (self.input / m.INSTALLER).write_bytes(b'SYNTHETIC SCRIPT')
        self.ah = m.digest(self.input / m.ARCHIVE)
        self.ih = m.digest(self.input / m.INSTALLER)
        self.data = dict(version=m.VERSION, source_commit=m.SOURCE, archive=m.ARCHIVE, installer=m.INSTALLER,
                         archive_sha256=self.ah, installer_sha256=self.ih, ready_for_install=False, signature_created=False)
        self.save()
        self.proof = m.confirmation(True, 'a' * 64, '2026-01-01T00:00:00+06:00')
        for name, value in [('ARCHIVE_SHA', self.ah), ('INSTALLER_SHA', self.ih), ('TRUSTED_KEY_SHA', self.fp)]:
            p = patch.object(m, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.output = self.root_case / 'signed'

    def save(self):
        (self.input / m.METADATA).write_text(json.dumps(self.data))

    def test_valid_signature_does_not_mutate_original_or_copy_private_key(self):
        with contextlib.redirect_stdout(io.StringIO()):
            m.sign(self.input, self.output, self.key_text, self.proof)
            m.check_signed(self.output)
        self.assertFalse(json.loads((self.input / m.METADATA).read_text())['ready_for_install'])
        self.assertEqual(set(p.name for p in self.output.iterdir()), set(m.PUBLISHED_FILES) | {m.PUBLIC})
        for p in self.output.iterdir():
            self.assertNotIn(b'PRIVATE KEY', p.read_bytes())

    def test_wrong_key_fingerprint_refuses_without_output(self):
        with patch.object(m, 'TRUSTED_KEY_SHA', '0' * 64), self.assertRaises(m.Refusal):
            m.sign(self.input, self.output, self.key_text, self.proof)
        self.assertFalse(self.output.exists())

    def test_missing_key_refuses(self):
        with self.assertRaises(m.Refusal):
            m.sign(self.input, self.output, '', self.proof)

    def test_tampered_archive_refuses(self):
        (self.input / m.ARCHIVE).write_bytes(b'ALTERED')
        with self.assertRaises(m.Refusal):
            m.sign(self.input, self.output, self.key_text, self.proof)

    def test_unapproved_source_refuses(self):
        self.data['source_commit'] = 'b' * 40
        self.save()
        with self.assertRaises(m.Refusal):
            m.sign(self.input, self.output, self.key_text, self.proof)

    def test_claimed_ready_unsigned_candidate_refuses(self):
        self.data['ready_for_install'] = True
        self.save()
        with self.assertRaises(m.Refusal):
            m.sign(self.input, self.output, self.key_text, self.proof)

    def test_missing_restore_confirmation_refuses(self):
        for confirmed in (False, None, 'true'):
            with self.subTest(confirmed=confirmed), self.assertRaises(m.Refusal):
                m.confirmation(confirmed, 'a' * 64, '2026-01-01T00:00:00+06:00')
        with self.assertRaises(m.Refusal):
            m.sign(self.input, self.output, self.key_text, {})

    def test_invalid_backup_hash_and_future_time_refuse(self):
        for value in ('', '0' * 64, '$(bad)', '../private'):
            with self.subTest(hash=value), self.assertRaises(m.Refusal):
                m.confirmation(True, value, '2026-01-01T00:00:00+06:00')
        with self.assertRaises(m.Refusal):
            m.confirmation(True, 'a' * 64, '2026-01-01T00:00:00')
        with self.assertRaises(m.Refusal):
            m.confirmation(True, 'a' * 64, (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())

    def test_post_signing_modification_refuses(self):
        with contextlib.redirect_stdout(io.StringIO()):
            m.sign(self.input, self.output, self.key_text, self.proof)
        data = json.loads((self.output / m.METADATA).read_text())
        data['extra'] = 'modified'
        (self.output / m.METADATA).write_text(json.dumps(data))
        with self.assertRaises(m.Refusal):
            m.check_signed(self.output)

    def test_duplicate_manifest_entries_refuse(self):
        with contextlib.redirect_stdout(io.StringIO()):
            m.sign(self.input, self.output, self.key_text, self.proof)
        p = self.output / m.SUMS
        p.write_text(p.read_text() * 2)
        with self.assertRaises(m.Refusal):
            m.check_signed(self.output)

    def test_symlink_candidate_refuses(self):
        archive = self.input / m.ARCHIVE
        archive.unlink()
        target = self.root_case / 'outside'
        target.write_bytes(b'SYNTHETIC ZIP')
        archive.symlink_to(target)
        with self.assertRaises(m.Refusal):
            m.candidate(self.input)

    def test_cli_never_prints_key_or_private_errors(self):
        with patch.dict(m.os.environ, {'RESTORE_CONFIRMED': 'true', 'RESTORE_BACKUP_SHA256': 'a' * 64,
                        'RESTORE_VERIFIED_AT': '2026-01-01T00:00:00+06:00', 'ARMADA_RELEASE_SIGNING_KEY': 'PRIVATE_TEST_KEY'}), \
                patch('sys.argv', ['release', 'sign', '--directory', str(self.input), '--output', str(self.output)]), \
                patch.object(m, 'sign', side_effect=RuntimeError('PRIVATE_EXCEPTION_TEXT')):
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                self.assertEqual(m.main(), 1)
        self.assertNotIn('PRIVATE_', output.getvalue())

    def test_workflow_pins_actions_and_does_not_run_on_push(self):
        text = (ROOT / 'workflows/release-6.33.1.yml').read_text()
        self.assertIn('workflow_dispatch:', text)
        self.assertNotIn('pull_request:', text)
        self.assertNotIn('\n  push:', text)
        for line in text.splitlines():
            if 'uses:' in line:
                self.assertRegex(line, r'uses: actions/[a-z-]+@[0-9a-f]{40}')
        self.assertIn('environment: armada-release', text)
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertIn('git -C downloads push origin HEAD:refs/heads/downloads/nas-updates', text)
        self.assertNotIn('--force', text)


if __name__ == '__main__':
    unittest.main()
