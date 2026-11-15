"""Read-only list of recovery sets next to the application. It never deletes anything.

Classes: complete (phase=complete), failed (failure.txt or a post-barrier phase),
incomplete (any other phase), unknown (no readable phase). Only complete sets older
than the newest complete set are marked as possible manual-cleanup candidates.
"""
import argparse,json,os,stat
from pathlib import Path


def read(path):
    try:
        if path.is_symlink() or not path.is_file():return None
        return path.read_text().strip()
    except OSError:return None


def inventory(directory):
    directory=Path(directory);rows=[]
    for p in directory.glob('.armada-recovery-*'):
        st=p.lstat()
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):continue
        phase=read(p/'phase');failure=(p/'failure.txt').is_file()
        kind='complete' if phase=='complete' else 'failed' if failure or phase in ('migrate_started','migrate_done','verify') else 'incomplete' if phase else 'unknown'
        rows.append({'name':p.name,'phase':phase,'class':kind,'previous_version':read(p/'previous-version'),'has_archive':(p/'archive').is_file(),'has_local_files':(p/'local-files.txt').is_file(),'mtime':st.st_mtime,'mode':oct(stat.S_IMODE(st.st_mode)),'owner_uid':st.st_uid})
    rows.sort(key=lambda r:r['mtime'],reverse=True)
    newest=next((r for r in rows if r['class']=='complete'),None)
    for r in rows:
        r['keep_reason']=None if r['class']=='complete' and r is not newest else 'newest complete set' if r is newest else 'failed, incomplete or unknown set: keep'
        r['manual_cleanup_candidate']=r['keep_reason'] is None
    return rows


if __name__=='__main__':
    a=argparse.ArgumentParser(description='Только чтение: ничего не удаляет.');a.add_argument('--directory',type=Path,required=True);a.add_argument('--json',action='store_true');v=a.parse_args()
    rows=inventory(v.directory)
    if v.json:print(json.dumps(rows,ensure_ascii=False,indent=2))
    else:
        for r in rows:print('%s phase=%s class=%s archive=%s cleanup_candidate=%s'%(r['name'],r['phase'],r['class'],r['has_archive'],r['manual_cleanup_candidate']))
        print('Ничего не удалено. Каталоги содержат закрытые настройки; удалять только вручную после проверки.')
