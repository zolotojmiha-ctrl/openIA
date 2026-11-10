"""Prune only older-than-verified backups; no proof means no automatic deletion."""
import argparse,datetime,hashlib,json,os,re
from pathlib import Path
PATTERN=re.compile(r'armada-\d{8}T\d{6}Z(?:-[0-9a-f]{8})?\.tar\.gz(?:\.encrypted)?$')


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def prune(directory,keep=7,days=30,apply=False,now=None):
    if keep<1 or days<1:raise ValueError('Keep and days must be positive')
    directory=directory.resolve();os.chmod(directory,0o700)
    files=sorted((p for p in directory.iterdir() if PATTERN.fullmatch(p.name) and p.is_file() and not p.is_symlink()),key=lambda p:p.stat().st_mtime,reverse=True)
    verified=[]
    for p in files:
        receipt=Path(str(p)+'.verified.json')
        if receipt.is_symlink() or not receipt.is_file():continue
        try:
            data=json.loads(receipt.read_text())
            if data.get('restored') is True and data.get('sha256')==digest(p):verified.append(p)
        except (ValueError,OSError):continue
    if not verified:return []
    last=verified[0];cutoff=(now or datetime.datetime.now().timestamp())-days*86400
    candidates=[p for p in files[keep:] if p!=last and p.stat().st_mtime<min(cutoff,last.stat().st_mtime)]
    if apply:
        for p in candidates:
            p.unlink()
            for suffix in ('.verified.json','.key-id'):
                side=Path(str(p)+suffix)
                if not side.is_symlink():side.unlink(missing_ok=True)
    return candidates

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--directory',type=Path,required=True);a.add_argument('--keep',type=int,default=7);a.add_argument('--days',type=int,default=30);a.add_argument('--apply',action='store_true');v=a.parse_args()
    try:rows=prune(v.directory,v.keep,v.days,v.apply)
    except Exception:a.exit(1,'Retention failed; inspect configuration privately.\n')
    print(('Removed: ' if v.apply else 'Eligible: ')+str(len(rows)))
