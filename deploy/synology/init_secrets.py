"""Initialise isolated test secrets once; never print values or rotate on restart."""
import os
import secrets
from pathlib import Path

root=Path('/run/armada')
root.mkdir(parents=True,exist_ok=True)

def write_once(name,value):
    path=root/name
    try:
        with open(path,'x',opener=lambda p,flags:os.open(p,flags,0o600)) as f:
            f.write(value)
    except FileExistsError:
        pass
    owner=999 if name.startswith('postgres-') else 10001
    os.chown(path,owner,owner)
    os.chmod(path,0o600)
    return path.read_text().strip()

write_once('django-key',secrets.token_urlsafe(64))
write_once('postgres-admin-password',secrets.token_urlsafe(32))
password=write_once('postgres-app-password',secrets.token_urlsafe(32))
write_once('database-url',f'postgresql://armada_app:{password}@db:5432/armada')
print('Isolated configuration prepared. Secret values are not logged.')
