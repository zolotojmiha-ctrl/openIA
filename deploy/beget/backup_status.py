"""Machine-readable backup status. Reads names, sizes and receipts only; never opens archive contents."""
import argparse,datetime,json,os,tempfile
from pathlib import Path
from backup_retention import copies,has_receipt

OMSK=datetime.timezone(datetime.timedelta(hours=6))


def status(directory,archive,encrypted,keep,now=None):
    directory=Path(directory).resolve();files=copies(directory)
    verified=[p for p in files if has_receipt(p)]
    newest_verified=verified[0] if verified else None
    unverified=[p for p in files if newest_verified is None or p.stat().st_mtime>newest_verified.stat().st_mtime]
    warnings=[]
    if not encrypted:warnings.append({'code':'unencrypted_backup','message':'Последняя копия не зашифрована.'})
    if len(unverified)>keep:warnings.append({'code':'too_many_unverified_copies','message':'Копий без подтверждённого восстановления больше порога; автоматическое удаление без receipt не выполняется.','count':len(unverified),'threshold':keep})
    moment=now or datetime.datetime.now(datetime.timezone.utc)
    return {'schema':1,'generated_at_utc':moment.isoformat(timespec='seconds'),'generated_at_omsk':moment.astimezone(OMSK).isoformat(timespec='seconds'),'timezone_user':'Asia/Omsk (UTC+06:00)',
            'last_backup':Path(archive).name,'last_backup_encrypted':bool(encrypted),'copies_total':len(files),'copies_with_receipt':len(verified),
            'newest_verified':newest_verified.name if newest_verified else None,'copies_without_receipt_since_newest_verified':len(unverified),
            'unverified_threshold':keep,'warnings':warnings}


def write(directory,data):
    directory=Path(directory)
    fd,name=tempfile.mkstemp(prefix='.backup-status.',dir=directory)
    with os.fdopen(fd,'w') as f:json.dump(data,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    os.chmod(name,0o600);os.replace(name,directory/'backup-status.json')


if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--directory',type=Path,required=True);a.add_argument('--archive',required=True);a.add_argument('--encrypted',type=int,choices=(0,1),required=True);a.add_argument('--keep',type=int,required=True);v=a.parse_args()
    data=status(v.directory,v.archive,v.encrypted,v.keep);write(v.directory,data)
    for w in data['warnings']:print('ПРЕДУПРЕЖДЕНИЕ ['+w['code']+']: '+w['message'],file=__import__('sys').stderr)
