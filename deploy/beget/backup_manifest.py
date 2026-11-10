"""Private archive content manifest. Never print filenames or data to logs."""
import argparse,hashlib,json,tarfile
from pathlib import Path,PurePosixPath


def contents(path):
    result={}
    with tarfile.open(path) as tar:
        for item in tar:
            name=str(PurePosixPath(item.name))
            if name=='.' and item.isdir():continue
            if name.startswith('/') or '..' in PurePosixPath(name).parts or name in result:raise ValueError('Unsafe archive member')
            if item.isdir():continue
            if not item.isfile():raise ValueError('Non-regular backup content')
            h=hashlib.sha256()
            with tar.extractfile(item) as stream:
                for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
            result[name]={'sha256':h.hexdigest(),'size':item.size,'mode':item.mode,'uid':item.uid,'gid':item.gid}
    return result


def capture(directory):
    counts=json.loads((directory/'row-counts.json').read_text())
    if not isinstance(counts,dict) or any(type(n)!=int or n<0 for n in counts.values()):raise ValueError('Invalid row counts')
    value={n:contents(directory/n) for n in ('config.tar','documents.tar')}
    (directory/'content-sha256.json').write_text(json.dumps(value,sort_keys=True))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True);a=p.parse_args()
    try:capture(a.directory)
    except Exception:p.exit(1,'Backup content validation failed; no data shown.\n')
