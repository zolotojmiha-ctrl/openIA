"""Synthetic policy/crypto tests. No service configuration or real keys."""
import hashlib,importlib.util,json,os,sys,tempfile,time,unittest
from pathlib import Path
REPO=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(REPO/'deploy/beget'),str(REPO/'server')]
from backup_retention import prune
from projects import disk_crypto

class BackupPolicyTests(unittest.TestCase):
 def make(self,root,index,age,verified=False):
  p=root/f'armada-202601{index:02d}T000000Z.tar.gz';p.write_bytes(f'SYNTHETIC {index}'.encode());t=time.time()-age*86400;os.utime(p,(t,t))
  if verified:Path(str(p)+'.verified.json').write_text(json.dumps({'restored':True,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}))
  return p
 def test_no_deletion_without_restored_proof(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td);self.make(r,1,100);self.make(r,2,99)
   self.assertEqual(prune(r,1,1,True),[]);self.assertEqual(len(list(r.glob('*.tar.gz'))),2)
 def test_keep_last_restored_and_all_newer_backups(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td);old=self.make(r,1,100);proof=self.make(r,2,90,True);new=self.make(r,3,80)
   self.assertEqual(prune(r,1,30,True),[old]);self.assertTrue(proof.exists());self.assertTrue(new.exists());self.assertEqual(r.stat().st_mode&0o777,0o700)
 def test_keep_count_and_age_are_both_required(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td);old=self.make(r,1,100);self.make(r,2,90,True)
   self.assertEqual(prune(r,2,1,True),[]);self.assertTrue(old.exists())
   self.assertEqual(prune(r,1,200,True),[])
 def test_tampered_proof_does_not_enable_deletion(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td);self.make(r,1,100);p=self.make(r,2,90,True);p.write_bytes(b'MODIFIED')
   self.assertEqual(prune(r,1,1,True),[])
 def test_symlink_and_unrelated_files_are_not_deleted(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td);outside=r/'not-a-backup';outside.write_text('KEEP');(r/'armada-20260101T000000Z.tar.gz').symlink_to(outside);self.make(r,2,1,True)
   self.assertEqual(prune(r,1,1,True),[]);self.assertTrue(outside.exists())
 def test_encrypted_archive_roundtrip_and_wrong_key(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td);source=r/'source';source.write_bytes(b'SYNTHETIC BACKUP'*10000)
   private,public,fingerprint=disk_crypto.generate();encrypted=r/'backup.encrypted';disk_crypto.encrypt(source,encrypted,public)
   output=r/'restored';disk_crypto.decrypt(encrypted,output,private);self.assertEqual(output.read_bytes(),source.read_bytes())
   wrong,_,_=disk_crypto.generate()
   with self.assertRaises(Exception):disk_crypto.decrypt(encrypted,r/'wrong',wrong)
   self.assertFalse((r/'wrong').exists())
 def test_corrupt_ciphertext_has_no_published_plaintext(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td);source=r/'source';source.write_bytes(b'SYNTHETIC BACKUP');private,public,_=disk_crypto.generate();encrypted=r/'backup.encrypted';disk_crypto.encrypt(source,encrypted,public)
   b=bytearray(encrypted.read_bytes());b[-1]^=1;encrypted.write_bytes(b)
   with self.assertRaises(Exception):disk_crypto.decrypt(encrypted,r/'result',private)
   self.assertFalse((r/'result').exists());self.assertFalse((r/'result.partial').exists())

if __name__=='__main__':unittest.main(verbosity=2)
