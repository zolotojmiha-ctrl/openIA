#!/usr/bin/env python3
"""Restore on a trusted recovery machine only. Never use production DB/volumes."""
import argparse,hashlib,json,os,subprocess,sys,tarfile,tempfile,time,uuid
from pathlib import Path
from backup_manifest import contents


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def verify(archive,receipt,private=None,image='postgres:17-bookworm'):
    archive=archive.resolve()
    env=os.environ.copy()
    for k in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):env.pop(k,None)
    cli=['docker','--host=unix:///var/run/docker.sock']
    name='armada-verify-'+uuid.uuid4().hex
    def run(args,**kw):
        r=subprocess.run(cli+args,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=180,**kw)
        if r.returncode:raise RuntimeError('Isolated restore command failed; no private output shown')
        return r.stdout
    with tempfile.TemporaryDirectory(prefix='armada-verify-') as td:
        work=Path(td);work.chmod(0o700);source=archive
        if archive.name.endswith('.encrypted'):
            if not private:raise ValueError('Offline recovery key required')
            sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'server'))
            from projects.disk_crypto import decrypt
            source=work/'plain.tar.gz';decrypt(archive,source,Path(private).read_bytes())
        required={'database.dump','config.tar','documents.tar','row-counts.json','content-sha256.json','SHA256SUMS'}
        with tarfile.open(source) as tar:
            seen=set()
            for member in tar:
                parts=Path(member.name).parts
                if member.name.startswith('/') or '..' in parts:raise ValueError('Unsafe archive')
                name=str(Path(member.name))
                if name=='.' and member.isdir():continue
                if name in seen or not member.isfile():raise ValueError('Invalid archive')
                seen.add(name)
                if name in required:
                    with tar.extractfile(member) as src,(work/name).open('xb') as dst:
                        __import__('shutil').copyfileobj(src,dst)
            if not required<=seen:raise ValueError('Legacy/incomplete copy needs manual verification; no receipt issued')
        hashes={}
        for line in (work/'SHA256SUMS').read_text().splitlines():
            digest,name=line.split(None,1);name=name.strip().lstrip('*')
            if name not in required or name in hashes:raise ValueError('Invalid checksum manifest')
            hashes[name]=digest
            if sha(work/name)!=digest:raise ValueError('Checksum mismatch')
        if not required-{'SHA256SUMS'}<=hashes.keys():raise ValueError('Missing checksums')
        expected=json.loads((work/'content-sha256.json').read_text())
        for file in ('config.tar','documents.tar'):
            if contents(work/file)!=expected[file]:raise ValueError('Private file content/metadata mismatch')
        counts=json.loads((work/'row-counts.json').read_text())
        # The recovered files really get materialized on this private temporary
        # filesystem, never in a production Docker volume.
        for file in ('config.tar','documents.tar'):
            target=work/file.removesuffix('.tar');target.mkdir()
            with tarfile.open(work/file) as tar:tar.extractall(target,filter='data')
            for name,info in expected[file].items():
                if sha(target/name)!=info['sha256']:raise ValueError('Restored file mismatch')
        try:
            run(['run','--detach','--pull','never','--name',name,'--network','none','--memory','512m','--env','POSTGRES_HOST_AUTH_METHOD=trust','--tmpfs','/var/lib/postgresql/data:rw,size=768m',image,'-c','shared_buffers=32MB','-c','max_wal_size=64MB'])
            ready=False
            for _ in range(80):
                try:run(['exec',name,'pg_isready','-U','postgres']);ready=True;break
                except RuntimeError:time.sleep(.25)
            if not ready:raise RuntimeError('Isolated PostgreSQL did not start')
            with (work/'database.dump').open('rb') as dump:run(['exec','-i',name,'pg_restore','--exit-on-error','--no-owner','--no-acl','-U','postgres','-d','postgres'],stdin=dump)
            sql=(Path(__file__).with_name('backup_counts.sql')).read_text()
            actual=json.loads(run(['exec',name,'psql','-U','postgres','-d','postgres','-At','-c',sql]))
            if actual!=counts:raise ValueError('Restored row counts differ')
        finally:
            subprocess.run(cli+['rm','--force','--volumes',name],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
        result={'restored':True,'sha256':sha(archive),'verified_at':int(time.time()),'tables':len(counts),'files':sum(map(len,expected.values()))}
        # No key, row details, paths from documents or production credentials.
        fd=os.open(receipt,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:json.dump(result,f);f.flush();os.fsync(f.fileno())
        return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--archive',type=Path,required=True);p.add_argument('--receipt',type=Path,required=True);p.add_argument('--private-key',type=Path);p.add_argument('--postgres-image',default='postgres:17-bookworm');a=p.parse_args()
    try:verify(a.archive,a.receipt,a.private_key,a.postgres_image)
    except Exception:p.exit(1,'Проверка восстановления не завершена; подтверждение не выдано. Данные и ключ не выводятся.\n')
    print('Изолированное восстановление базы и файлов подтверждено.')
