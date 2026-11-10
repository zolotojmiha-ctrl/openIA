#!/usr/bin/env python3
"""Sign immutable reviewed artifacts. Signing key is supplied as a private local file."""
import argparse,hashlib,json,os,re,subprocess,tempfile
from pathlib import Path


def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def sign(directory,version,key):
    if not re.fullmatch(r'\d+\.\d+\.\d+',version):raise ValueError('Invalid version')
    manifest=directory/f'SHA256SUMS-{version}'
    files=[directory/f'openIA-{version}-update.zip',directory/f'install-{version}.sh',directory/f'openIA-{version}-update.json']
    data=json.loads(files[2].read_text())
    if data.get('version')!=version or data.get('ready_for_install') is not True:raise ValueError('Finish validation before signing')
    if sha(files[0])!=data['archive_sha256'] or sha(files[1])!=data['installer_sha256']:raise ValueError('Artifact mismatch')
    if any(not p.is_file() or p.is_symlink() for p in files):raise ValueError('Unsafe artifact')
    public=subprocess.run(['openssl','pkey','-in',str(key),'-pubout','-outform','DER'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if public.returncode:raise ValueError('Signing identity unavailable')
    data['signature_created']=True
    data['signing_public_key_sha256']=hashlib.sha256(public.stdout).hexdigest()
    metadata=(json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode()
    content=''.join((hashlib.sha256(metadata).hexdigest() if p==files[2] else sha(p))+'  '+p.name+'\n' for p in files)
    with tempfile.TemporaryDirectory(prefix='armada-sign-') as td:
        work=Path(td);candidate=work/'SHA256SUMS';candidate.write_text(content)
        result=subprocess.run(['openssl','dgst','-sha256','-sign',str(key),'-out',str(candidate)+'.sig',str(candidate)],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        if result.returncode:raise ValueError('Signing failed')
        signature=Path(str(candidate)+'.sig').read_bytes()
    files[2].write_bytes(metadata)
    manifest.write_text(content);Path(str(manifest)+'.sig').write_bytes(signature)
    (directory/'SHA256SUMS').write_text(content)
    (directory/'SHA256SUMS.sig').write_bytes(signature)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True);p.add_argument('--version',required=True);p.add_argument('--key-file',type=Path,required=True);a=p.parse_args()
    try:sign(a.directory,a.version,a.key_file)
    except Exception:p.exit(1,'Подпись не создана. Проверьте итоговые артефакты и локальный ключ; секреты не выводятся.\n')
    print('Подписанный манифест создан. Публичный ключ передать независимым доверенным способом.')
