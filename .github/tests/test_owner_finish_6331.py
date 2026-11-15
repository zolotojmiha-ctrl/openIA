import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('owner_finish', ROOT / 'tools/owner_finish_6331.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class OwnerFinishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='armada-owner-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.unsigned = self.root / 'unsigned'
        self.unsigned.mkdir()
        (self.unsigned / m.ARCHIVE).write_bytes(b'SYNTHETIC ZIP')
        (self.unsigned / m.INSTALLER).write_bytes(b'SYNTHETIC SCRIPT')
        self.ah = m.digest(self.unsigned / m.ARCHIVE)
        self.ih = m.digest(self.unsigned / m.INSTALLER)
        (self.unsigned / m.METADATA).write_text(json.dumps(dict(version=m.VERSION, source_commit=m.SOURCE,
            archive=m.ARCHIVE, installer=m.INSTALLER, archive_sha256=self.ah, installer_sha256=self.ih,
            ready_for_install=False, signature_created=False)))
        self.key = self.root / 'test-signing.pem'
        subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',str(self.key)], check=True, capture_output=True)
        fp = hashlib.sha256(m.openssl('pkey','-in',self.key,'-pubout','-outform','DER')).hexdigest()
        for name,value in [('ARCHIVE_SHA',self.ah),('INSTALLER_SHA',self.ih),('TRUSTED_KEY_SHA',fp)]:
            p=patch.object(m,name,value);p.start();self.addCleanup(p.stop)
        self.proof=m.confirmation(True,'a'*64,'2026-01-01T00:00:00+06:00')

    def signed(self):
        out=self.root/'signed'
        with contextlib.redirect_stdout(io.StringIO()):
            m.sign(self.unsigned,out,self.key.read_text(),self.proof)
        (out / 'backup-recovery.public.pem').write_bytes(m.openssl('pkey','-in',self.key,'-pubout'))
        return out

    def test_small_public_response_reconstructs_identical_outer_without_private_data(self):
        signed=self.signed()
        first=self.root/m.OUTER;second=self.root/'second.zip'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.outer_package(signed,first),m.outer_package(signed,second))
        response=self.root/'public-response.zip'
        m.public_response(signed,first,self.key,response)
        received=self.root/'received';received.mkdir()
        for name in (m.ARCHIVE,m.INSTALLER):
            (received/name).write_bytes((self.unsigned/name).read_bytes())
        with zipfile.ZipFile(response) as z:
            self.assertEqual(set(z.namelist()),set(m.RESPONSE_FILES))
            for name in z.namelist():self.assertNotIn(b'PRIVATE KEY',z.read(name))
            z.extractall(received)
        with contextlib.redirect_stdout(io.StringIO()):
            m.check_signed(received)
            rebuilt=self.root/'rebuilt.zip';m.outer_package(received,rebuilt)
        self.assertEqual(first.read_bytes(),rebuilt.read_bytes())
        m.openssl('dgst','-sha256','-verify',received/m.PUBLIC,'-signature',received/(m.OUTER+'.sig'),rebuilt)
        self.assertLess(response.stat().st_size,1048576)
        import sys
        with patch.dict(sys.modules, {'owner_finish_6331': m}):
            spec=importlib.util.spec_from_file_location('assemble',ROOT/'tools/assemble_signed_6331.py')
            assembler=importlib.util.module_from_spec(spec);spec.loader.exec_module(assembler)
        result=self.root/'assembled'
        with contextlib.redirect_stdout(io.StringIO()):assembler.assemble(response,self.unsigned,result)
        self.assertEqual((result/m.OUTER).read_bytes(),first.read_bytes())
        self.assertTrue(json.loads((result/'VERIFIED.json').read_text())['outer_signature_verified'])


    def test_restore_rejects_archive_inside_build_context(self):
        code=self.root/'code';code.mkdir();backup=code/'copy.encrypted';backup.write_bytes(b'PRIVATE')
        with patch.object(m.subprocess,'run') as run,self.assertRaises(m.Refusal):
            m.restore([],{},code,backup,self.key,self.root,'a'*64)
        run.assert_not_called()

    def test_restore_rejects_failed_or_wrong_receipt(self):
        code=self.root/'code';code.mkdir();backup=self.root/'copy.encrypted';backup.write_bytes(b'PRIVATE')
        receipts=self.root/'receipts';receipts.mkdir()
        def run(*args,**kwargs):
            if 'run' in args[0]:
                (receipts/'restored-real-backup.json').write_text(json.dumps(dict(restored=True,sha256='0'*64,tables=1,verified_at=1)))
            return subprocess.CompletedProcess(args[0],0)
        with patch.object(m.subprocess,'run',side_effect=run),self.assertRaises(m.Refusal):
            m.restore(['docker'],{},code,backup,self.key,receipts,m.digest(backup))

    def test_failed_restore_never_reads_signing_key_or_calls_sign(self):
        backup=self.root/'copy.encrypted';backup.write_bytes(b'PRIVATE BACKUP')
        recovery=self.root/'recovery.pem';recovery.write_text('PRIVATE RECOVERY')
        def download(name,target):target.write_bytes((self.unsigned/name).read_bytes())
        with patch('platform.system',return_value='Darwin'),patch.object(m.Path,'home',return_value=self.root), \
             patch.object(m,'desktop',return_value=([],{})),patch.object(m,'choose_file',side_effect=[backup,recovery,self.key]), \
             patch.object(m,'download',side_effect=download),patch.object(m,'extract_code',return_value=self.root/'code'), \
             patch.object(m,'restore',side_effect=m.Refusal('restore failed')),patch.object(m,'sign') as sign, \
             contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(m.owner_main(),1)
        sign.assert_not_called()
        self.assertFalse(list((self.root/'Downloads').rglob('*public-signatures.zip')))

    def test_private_restore_inputs_never_in_outer(self):
        signed=self.signed();(signed/'database.dump').write_bytes(b'PRIVATE DATA')
        (signed/'private-signing.pem').write_text('PRIVATE KEY')
        target=self.root/'outer.zip'
        with contextlib.redirect_stdout(io.StringIO()):m.outer_package(signed,target)
        with zipfile.ZipFile(target) as z:
            self.assertEqual(set(z.namelist()),{m.PREFIX+n for n in m.OUTER_FILES})
            self.assertNotIn(b'PRIVATE KEY',z.read(m.PREFIX+'armada-release-signing.public.pem'))

if __name__=='__main__':unittest.main()
