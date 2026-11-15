"""Synthetic policy/crypto tests. No service configuration or real keys."""
import hashlib,importlib.util,json,os,sys,tempfile,time,unittest
from pathlib import Path
REPO=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(REPO/'deploy/beget'),str(REPO/'server')]
from backup_retention import prune,has_receipt
from backup_status import status,write
from projects import disk_crypto

class BackupPolicyTests(unittest.TestCase):
 def make(self,root,index,age,verified=False):
  p=root/f'armada-202601{index:02d}T000000Z.tar.gz';p.write_bytes(f'SYNTHETIC {index}'.encode());t=time.time()-age*86400;os.utime(p,(t,t))
  if verified:Path(str(p)+'.verified.json').write_text(json.dumps({'restored':True,'archive':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}))
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

 def test_receipt_is_bound_to_its_own_copy_and_checksum(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td);a=self.make(r,1,100,True);b=self.make(r,2,90)
   # Copying copy A's receipt next to copy B (same checksum of a different file) is not accepted.
   Path(str(b)+'.verified.json').write_text(Path(str(a)+'.verified.json').read_text())
   self.assertTrue(has_receipt(a));self.assertFalse(has_receipt(b))
   # Same bytes but a different recorded copy name is not accepted either.
   Path(str(b)+'.verified.json').write_text(json.dumps({'restored':True,'archive':a.name,'sha256':hashlib.sha256(b.read_bytes()).hexdigest()}))
   self.assertFalse(has_receipt(b))
   # A legacy receipt without the copy name is conservative: no deletion.
   Path(str(a)+'.verified.json').write_text(json.dumps({'restored':True,'sha256':hashlib.sha256(a.read_bytes()).hexdigest()}))
   self.assertFalse(has_receipt(a));self.assertEqual(prune(r,1,1,True),[]);self.assertTrue(a.exists() and b.exists())
 def test_status_warns_above_threshold_without_receipt_and_is_machine_readable(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td)
   for i in range(1,5):self.make(r,i,10-i)
   data=status(r,'armada-20260104T000000Z.tar.gz',1,3)
   self.assertEqual([w['code'] for w in data['warnings']],['too_many_unverified_copies']);self.assertEqual(data['copies_without_receipt_since_newest_verified'],4)
   write(r,data);self.assertEqual(json.loads((r/'backup-status.json').read_text())['schema'],1);self.assertEqual((r/'backup-status.json').stat().st_mode&0o777,0o600)
   self.assertEqual(len(list(r.glob('*.tar.gz'))),4)  # status never deletes
   self.make(r,9,1,True)
   self.assertEqual(status(r,'x',1,3)['warnings'],[])
 def test_unencrypted_status_warning(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td);self.make(r,1,1,True);self.assertEqual([w['code'] for w in status(r,'x',0,7)['warnings']],['unencrypted_backup'])

if __name__=='__main__':unittest.main(verbosity=2)
