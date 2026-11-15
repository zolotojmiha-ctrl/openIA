"""Exercise the real publication shell against local synthetic Git repositories."""
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / 'history/release-6.33.1.yml.disabled'
FILES = ('openIA-6.33.1-update.zip', 'install-6.33.1.sh', 'openIA-6.33.1-update.json', 'SHA256SUMS-6.33.1', 'SHA256SUMS-6.33.1.sig')


def git(directory, *args):
    return subprocess.check_output(['git', '-C', str(directory), *args], stderr=subprocess.PIPE).decode().strip()


class PublicationTests(unittest.TestCase):
    def test_sparse_publication_preserves_history_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory(prefix='armada-publish-proof-') as td:
            root = Path(td)
            origin = root / 'remote.git'
            initial = root / 'initial'
            git(root, 'init', '--bare', str(origin))
            git(root, 'init', '-b', 'downloads/nas-updates', str(initial))
            git(initial, 'config', 'user.name', 'Synthetic fixture')
            git(initial, 'config', 'user.email', 'fixture@example.invalid')
            (initial / 'previous-release.zip').write_bytes(b'PREVIOUS RELEASE MUST REMAIN')
            git(initial, 'add', 'previous-release.zip')
            git(initial, 'commit', '-m', 'Synthetic older release')
            git(initial, 'remote', 'add', 'origin', str(origin))
            git(initial, 'push', 'origin', 'HEAD:refs/heads/downloads/nas-updates')
            git(root, 'clone', '--no-checkout', '--branch', 'downloads/nas-updates', str(origin), str(root / 'downloads'))
            git(root / 'downloads', 'sparse-checkout', 'set', '--no-cone', *('/' + name for name in FILES))
            git(root / 'downloads', 'checkout', 'downloads/nas-updates')
            signed = root / 'signed'
            signed.mkdir()
            for name in FILES:
                (signed / name).write_bytes(('SYNTHETIC PUBLIC ' + name).encode())
            (signed / 'verification-public.pem').write_text('PUBLIC TEST EXTRA NOT FOR DOWNLOAD BRANCH')
            (signed / 'private-extra.pem').write_text('TEST EXTRA MUST NOT BE COPIED')
            marker = '      - name: Publish new version without overwriting historical files\n        run: |\n'
            shell = textwrap.dedent(WORKFLOW.read_text().split(marker, 1)[1])
            env = os.environ | {'GITHUB_STEP_SUMMARY': str(root / 'summary.md')}
            before_blob = git(origin, 'rev-parse', 'refs/heads/downloads/nas-updates:previous-release.zip')
            result = subprocess.run(['bash', '-c', shell], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(git(origin, 'rev-parse', 'refs/heads/downloads/nas-updates:previous-release.zip'), before_blob)
            self.assertEqual(set(git(origin, 'ls-tree', '--name-only', 'refs/heads/downloads/nas-updates').splitlines()), set(FILES) | {'previous-release.zip'})
            published = git(origin, 'rev-parse', 'refs/heads/downloads/nas-updates')
            again = subprocess.run(['bash', '-c', shell], cwd=root, env=env, capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)
            self.assertIn('Перезапись запрещена', again.stderr)
            self.assertEqual(git(origin, 'rev-parse', 'refs/heads/downloads/nas-updates'), published)


if __name__ == '__main__':
    unittest.main()
