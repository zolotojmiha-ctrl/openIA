import io,json,os,subprocess,sys,tarfile,tempfile,time,uuid,hashlib
from pathlib import Path
repo=Path(__file__).resolve().parents[1];sys.path[:0]=[str(repo/'server'),str(repo/'deploy/beget')]
from projects import disk_crypto
from backup_manifest import capture
from verify_backup import verify
cli=['docker','--host=unix:///var/run/docker.sock'];env=os.environ.copy()
for k in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):env.pop(k,None)
name='armada-backup-fixture-'+uuid.uuid4().hex[:8]
def run(args,**kw):return subprocess.run(cli+args,env=env,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=180,**kw).stdout
with tempfile.TemporaryDirectory(prefix='armada-backup-proof-') as td:
 root=Path(td);stage=root/'staging';stage.mkdir();stage.chmod(0o700)
 try:
  run(['run','--detach','--pull','never','--name',name,'--network','none','--env','POSTGRES_HOST_AUTH_METHOD=trust','--tmpfs','/var/lib/postgresql/data:rw,size=512m','postgres:17-bookworm','-c','shared_buffers=32MB','-c','max_wal_size=64MB'])
  for _ in range(80):
   try:run(['exec',name,'pg_isready','-U','postgres']);break
   except subprocess.CalledProcessError:time.sleep(.25)
  run(['exec',name,'psql','-U','postgres','-c',"CREATE TABLE synthetic(id integer PRIMARY KEY, label text); INSERT INTO synthetic VALUES (1,'TEST'),(2,'TEST');"])
  (stage/'database.dump').write_bytes(run(['exec',name,'pg_dump','-U','postgres','-Fc','--no-owner','--no-acl','postgres']))
  sql=(repo/'deploy/beget/backup_counts.sql').read_text();(stage/'row-counts.json').write_bytes(run(['exec',name,'psql','-U','postgres','-At','-c',sql]))
 finally:subprocess.run(cli+['rm','--force','--volumes',name],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 for file in ('config.tar','documents.tar'):
  with tarfile.open(stage/file,'w') as tar:
   data=b'SYNTHETIC ONLY';i=tarfile.TarInfo('fixture');i.size=len(data);i.mode=0o600;i.uid=i.gid=10001;tar.addfile(i,io.BytesIO(data))
 capture(stage)
 names=['database.dump','config.tar','documents.tar','row-counts.json','content-sha256.json']
 (stage/'SHA256SUMS').write_text(''.join(hashlib.sha256((stage/n).read_bytes()).hexdigest()+'  '+n+'\n' for n in names))
 archive=root/'armada-20261110T000000Z.tar.gz'
 with tarfile.open(archive,'w:gz') as tar:
  for p in stage.iterdir():tar.add(p,arcname=p.name)
 private,public,_=disk_crypto.generate();key=root/'recovery.pem';key.write_bytes(private);key.chmod(0o600)
 encrypted=Path(str(archive)+'.encrypted');disk_crypto.encrypt(archive,encrypted,public)
 result=verify(encrypted,root/'receipt.json',key)
 assert result['restored'] and result['tables']==1 and result['files']==2
 print('PASS encrypted archive: actual PostgreSQL 17 restore, row counts, extracted file hashes')
 # Count mismatch must not produce a success receipt.
 (stage/'row-counts.json').write_text('{"public.synthetic":999}')
 capture(stage);(stage/'SHA256SUMS').write_text(''.join(hashlib.sha256((stage/n).read_bytes()).hexdigest()+'  '+n+'\n' for n in names))
 bad=root/'mismatch.tar.gz'
 with tarfile.open(bad,'w:gz') as tar:
  for p in stage.iterdir():tar.add(p,arcname=p.name)
 try:verify(bad,root/'bad-receipt.json')
 except ValueError:pass
 else:raise AssertionError('Count mismatch accepted')
 assert not (root/'bad-receipt.json').exists()
 print('PASS restore rejection: incorrect row counts issue no receipt')
