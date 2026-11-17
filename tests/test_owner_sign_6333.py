"""Real temporary cryptography; no owner keys, uploads or production data."""
import hashlib,importlib.util,json,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import owner_sign_6333 as release
import assemble_signed_6333 as assembly


class OwnerSigningTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.key=self.root/'synthetic-key.pem'
        subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',str(self.key)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.baseline=self.root/'baseline';self.baseline.mkdir()
        public=release.openssl('pkey','-in',self.key,'-pubout')
        (self.baseline/release.PUBLIC).write_bytes(public)
        (self.baseline/release.BACKUP).write_text('SYNTHETIC PUBLIC KEY FOR OFFLINE TEST\n')
        self.fingerprint=release.fingerprint(self.baseline/release.PUBLIC)
        self.backup_sha=release.sha(self.baseline/release.BACKUP)
        prior=self.baseline/'openIA-6.33.1-update.json'
        prior.write_text(json.dumps({'version':'6.33.1','owner_restore_confirmation':{'restored':True,'backup_sha256':'a'*64,'verified_at':'2026-11-15T10:00:00+00:00'}}))
        sums=self.baseline/'SHA256SUMS-6.33.1';sums.write_text(release.sha(prior)+'  '+prior.name+'\n')
        release.openssl('dgst','-sha256','-sign',self.key,'-out',str(sums)+'.sig',sums)
        self.candidate=self.root/'candidate';self.candidate.mkdir()
        (self.candidate/release.ARCHIVE).write_bytes(b'SYNTHETIC ZIP')
        (self.candidate/release.INSTALLER).write_bytes(b'SYNTHETIC INSTALLER')
        (self.candidate/release.META).write_text(json.dumps({'version':release.VERSION,'source_commit':'b'*40,'ready_for_install':False,'signature_created':False}))
        self.archive_sha=release.sha(self.candidate/release.ARCHIVE);self.installer_sha=release.sha(self.candidate/release.INSTALLER)
        for name,value in [('TRUSTED_KEY_SHA',self.fingerprint),('BACKUP_PUBLIC_SHA',self.backup_sha)]:
            p=patch.object(release,name,value);p.start();self.addCleanup(p.stop)

    def sign(self):return release.sign(self.candidate,self.root/'signed',self.key,self.baseline,self.archive_sha,self.installer_sha,'b'*40)

    def test_sign_only_reuses_prior_proof_and_public_archive_reproduces_exactly(self):
        response=self.sign()
        signed=self.root/'signed'
        metadata=json.loads((signed/release.META).read_text())
        self.assertTrue(metadata['ready_for_install'])
        self.assertEqual(metadata['restore_validation_basis']['release'],'6.33.1')
        self.assertEqual(metadata['owner_restore_confirmation']['verified_at'],'2026-11-15T10:00:00+00:00')
        import zipfile
        with zipfile.ZipFile(response) as z:
            self.assertEqual(set(z.namelist()),set(release.RESPONSE_FILES))
            self.assertFalse(any(b'PRIVATE KEY' in z.read(name) for name in z.namelist()))
        with patch.object(release,'prior_confirmation',return_value=(metadata['owner_restore_confirmation'],'synthetic')):
            output=assembly.assemble(self.candidate,response,self.root/'published','b'*40)
        self.assertEqual(release.sha(output),release.sha(signed/release.OUTER))

    def test_changed_candidate_is_rejected_before_signing(self):
        (self.candidate/release.ARCHIVE).write_bytes(b'ALTERED')
        with self.assertRaises(ValueError):self.sign()
        self.assertFalse((self.root/'signed').exists())

    def test_changed_prior_metadata_is_rejected(self):
        (self.baseline/'openIA-6.33.1-update.json').write_text('{}')
        with self.assertRaises(ValueError):self.sign()
        self.assertFalse((self.root/'signed').exists())

    def test_wrong_signing_identity_is_rejected_without_output(self):
        other=self.root/'different-key.pem'
        subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',str(other)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        with self.assertRaises(ValueError):release.sign(self.candidate,self.root/'signed',other,self.baseline,self.archive_sha,self.installer_sha,'b'*40)
        self.assertFalse((self.root/'signed').exists())

    def test_existing_output_is_not_overwritten(self):
        (self.root/'signed').mkdir()
        with self.assertRaises(ValueError):self.sign()


if __name__=='__main__':unittest.main()
