"""Replace only application code. Existing volumes and private dotenv files stay intact."""
import argparse
import os
from pathlib import Path
import shutil
import stat


def metadata(old,new):
    st=old.lstat()
    if stat.S_ISLNK(st.st_mode):
        raise ValueError('Existing code/runtime path is a symlink; manual review required')
    shutil.copystat(old,new,follow_symlinks=False)
    os.chown(new,st.st_uid,st.st_gid,follow_symlinks=False)


def prepare(source,app,recovery):
    target=recovery/'new-tree'
    if target.exists() or (recovery/'tree').exists():raise ValueError('Checkpoint already contains a tree')
    if app.stat().st_dev!=recovery.stat().st_dev:raise ValueError('Switch requires one filesystem')
    shutil.copytree(source,target)
    # Preserve metadata of still-existing files, but never resurrect removed code.
    for item in sorted(target.rglob('*'),reverse=True):
        relative=item.relative_to(target);old=app/relative
        if item.is_symlink():raise ValueError('Package symlink')
        if old.exists() or old.is_symlink():metadata(old,item)
    metadata(app,target)
    for directory in (Path('.'),Path('deploy/beget')):
        old=app/directory
        for secret in old.glob('.env*'):
            if secret.is_dir():raise ValueError('Unexpected runtime directory')
            new=target/directory/secret.name
            if secret.is_symlink():raise ValueError('Runtime dotenv symlink requires manual review')
            shutil.copy2(secret,new);metadata(secret,new)
    # The lock must have the SAME inode throughout the rename. A copied lock
    # would let a second updater acquire a different inode at the stable path.
    os.link(app/'.armada-update.lock',target/'.armada-update.lock')


def switch(app,recovery):
    os.rename(app,recovery/'tree')
    try:os.rename(recovery/'new-tree',app)
    except BaseException:
        os.rename(recovery/'tree',app)
        raise


def rollback(app,recovery):
    if (recovery/'tree').is_dir():
        if app.exists():os.rename(app,recovery/'failed-tree')
        os.rename(recovery/'tree',app)


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','switch','rollback'))
    p.add_argument('--app',type=Path,required=True);p.add_argument('--recovery',type=Path,required=True);p.add_argument('--source',type=Path)
    a=p.parse_args()
    if a.action=='prepare':prepare(a.source,a.app,a.recovery)
    elif a.action=='switch':switch(a.app,a.recovery)
    else:rollback(a.app,a.recovery)

if __name__=='__main__':main()
