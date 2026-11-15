"""Recovery container identity must never come from private archive filenames."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/beget'))
import backup_manifest
import verify_backup


class RestoreIsolationTests(unittest.TestCase):
    def test_nested_document_does_not_replace_unique_container_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stage = root / 'stage'
            stage.mkdir()
            for filename in ('config.tar', 'documents.tar'):
                with tarfile.open(stage / filename, 'w') as tar:
                    member = tarfile.TarInfo('private-folder/document.txt')
                    member.size = 4
                    tar.addfile(member, io.BytesIO(b'TEST'))
            (stage / 'database.dump').write_bytes(b'SYNTHETIC')
            (stage / 'row-counts.json').write_text('{}')
            backup_manifest.capture(stage)
            names = ('database.dump', 'config.tar', 'documents.tar', 'row-counts.json', 'content-sha256.json')
            (stage / 'SHA256SUMS').write_text(''.join(
                hashlib.sha256((stage / name).read_bytes()).hexdigest() + '  ' + name + '\n'
                for name in names))
            archive = root / 'backup.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                for path in stage.iterdir():
                    tar.add(path, arcname=path.name)
            calls = []

            def docker(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, b'{}', b'')

            with patch.object(verify_backup.subprocess, 'run', side_effect=docker):
                result = verify_backup.verify(archive, root / 'receipt.json')
            self.assertTrue(result['restored'])
            create = next(c for c in calls if '--name' in c)
            container = create[create.index('--name') + 1]
            self.assertRegex(container, r'^armada-verify-[0-9a-f]{32}$')
            self.assertTrue(all('private-folder/document.txt' not in c for c in calls))
            self.assertEqual(calls[-1][-1], container)
            for command in calls:
                if 'exec' in command:
                    self.assertIn(container, command)


if __name__ == '__main__':
    unittest.main()
