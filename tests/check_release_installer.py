"""Offline installer guard checks. Uses only fake Docker inside an isolated container."""
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import zipfile

repo=Path(__file__).resolve().parents[1]
base=repo/'.runtime/release630-installer'
base.mkdir(parents=True,exist_ok=True)
root=Path(tempfile.mkdtemp(prefix='guards-',dir=base))
version='6.30.0'
prefix='openIA-feature-accounts-foundation/'
required={'index.html':'NEW','server/config/settings.py':'',
          'server/accounts/context.py':"def local_testing(request): return {'release_version':'6.30.0'}",
          'deploy/synology/Dockerfile':'FROM scratch','deploy/beget/compose.yml':'NEW COMPOSE','deploy/beget/backup.sh':'NEW BACKUP'}
def archive(path,extra=None,wrong=False):
 with zipfile.ZipFile(path,'w') as z:
  for name,data in required.items():
   if wrong and name=='server/accounts/context.py':data=data.replace('6.30.0','6.29.0')
   z.writestr(prefix+name,data)
  if extra=='traversal':z.writestr(prefix+'../../bad','BAD')
  if extra=='symlink':
   link=zipfile.ZipInfo(prefix+'unsafe');link.create_system=3;link.external_attr=(stat.S_IFLNK|0o777)<<16;z.writestr(link,'/tmp/bad')
for name in ('valid','traversal','symlink','wrongversion'):
 archive(root/(name+'.zip'),extra=name,wrong=name=='wrongversion')
template=(repo/'deploy/beget/install-update.sh.in').read_text().replace('__VERSION__',version)
cases=['success','localarchive','relativearchive','badchecksum','wrongversion','traversal','symlink','buildfail','backupfail','healthfail','migrationfail','unsupported','updatelock','backuplock','maxon','lowspace','diskworkerlock']
for case in cases:
 selected=case if case in ('wrongversion','traversal','symlink') else 'valid'
 sha=hashlib.sha256((root/(selected+'.zip')).read_bytes()).hexdigest()
 (root/('installer-'+case+'.sh')).write_text(template.replace('__ARCHIVE_SHA256__',sha))
(root/'docker').write_text('''#!/bin/bash
printf '%s\\n' "$*" >> /tmp/install-smoke/commands
if [[ $1 == build && $CASE == buildfail ]]; then exit 17; fi
if [[ $1 == compose && $* == *' exec '* && $* == *'release_version'* ]]; then
 if [[ $CASE == unsupported ]]; then echo 6.24.0
 elif [[ $(cat "$ARMADA_APP_ROOT/index.html") == NEW || $CASE == maxon ]]; then echo 6.30.0
 else echo 6.29.0; fi
fi
if [[ $1 == compose && $* == *' config --services'* ]]; then printf 'web\\nmailworker\\nmaxworker\\nproxy\\n'; fi
if [[ $1 == compose && $* == *' ps --services '* ]]; then
 printf 'web\\nmailworker\\n'; [[ $CASE != maxon ]] || echo maxworker
fi
if [[ $1 == compose && $* == *' ps -q '* ]]; then echo "${!#}"; fi
if [[ $1 == compose && $* == *' migrate --check'* && $CASE == migrationfail ]]; then exit 18; fi
if [[ $1 == inspect ]]; then
 if [[ $* == *Health.Status* ]]; then [[ $CASE == healthfail ]] && echo unhealthy || echo healthy
 else echo true; fi
fi
exit 0
''')
(root/'curl').write_text('''#!/bin/bash
while (( $# )); do if [[ $1 == --output ]]; then output=$2; shift 2; else shift; fi; done
input=valid
case "$CASE" in traversal|symlink|wrongversion) input=$CASE;; esac
cp "/fixtures/$input.zip" "$output"
[[ $CASE != badchecksum ]] || printf 'corrupt' >> "$output"
''')
(root/'unzip').write_text('''#!/usr/local/bin/python3
import sys,zipfile
with zipfile.ZipFile(sys.argv[2]) as z:
 if sys.argv[1]=='-tq':assert z.testzip() is None
 else:z.extractall(sys.argv[4])
''')
(root/'sleep').write_text('#!/bin/sh\nexit 0\n')
(root/'df').write_text('''#!/bin/bash
if [[ $CASE == lowspace ]]; then printf 'Filesystem 1024-blocks Used Available Capacity Mounted\\nsynthetic 1000 900 100 90%% /\\n'; else exec /bin/df "$@"; fi
''')
(root/'run').write_text('''#!/bin/bash
set -eu
export PATH=/fixtures:$PATH
for CASE in success localarchive relativearchive badchecksum wrongversion traversal symlink buildfail backupfail healthfail migrationfail unsupported updatelock backuplock maxon lowspace diskworkerlock; do
 export CASE
 unset ARMADA_UPDATE_ARCHIVE
 rm -rf /tmp/install-smoke /tmp/relative-installer
 mkdir -p /tmp/install-smoke/openIA-feature-accounts-foundation/deploy/beget /tmp/install-smoke/backups
 export ARMADA_APP_ROOT=/tmp/install-smoke/openIA-feature-accounts-foundation
 export ARMADA_BACKUP_DIR=/tmp/install-smoke/backups
 printf OLD > "$ARMADA_APP_ROOT/index.html"
 printf COMPOSE > "$ARMADA_APP_ROOT/deploy/beget/compose.yml"
 printf PRESERVED > "$ARMADA_APP_ROOT/deploy/beget/.env"
 cat > "$ARMADA_APP_ROOT/deploy/beget/backup.sh" <<'SH'
#!/bin/bash
printf 'BACKUP\\n' >> /tmp/install-smoke/commands
[[ $CASE != backupfail ]]
SH
 if [[ $CASE == localarchive ]]; then export ARMADA_UPDATE_ARCHIVE=/fixtures/valid.zip; fi
 if [[ $CASE == updatelock ]]; then exec 7>"$ARMADA_APP_ROOT/.armada-update.lock"; flock -n 7; fi
 if [[ $CASE == diskworkerlock ]]; then exec 7>"$ARMADA_BACKUP_DIR/.disk-worker.lock"; flock -n 7; fi
 if [[ $CASE == backuplock ]]; then exec 7>"$ARMADA_BACKUP_DIR/.backup.lock"; flock -n 7; fi
 status=0
 if [[ $CASE == relativearchive ]]; then
  mkdir -p /tmp/relative-installer
  cp /fixtures/installer-relativearchive.sh /tmp/relative-installer/installer.sh
  cp /fixtures/valid.zip /tmp/relative-installer/openIA-6.30.0-update.zip
  (cd /tmp/relative-installer && bash ./installer.sh) > /tmp/install-smoke/output 2>&1 || status=$?
 else
  bash "/fixtures/installer-$CASE.sh" > /tmp/install-smoke/output 2>&1 || status=$?
 fi
 if [[ $CASE == updatelock || $CASE == backuplock || $CASE == diskworkerlock ]]; then flock -u 7; exec 7>&-; fi
 if [[ $CASE == success || $CASE == localarchive || $CASE == relativearchive || $CASE == maxon ]]; then
  [[ $status == 0 && $(cat "$ARMADA_APP_ROOT/index.html") == NEW ]]
  [[ $(cat "$ARMADA_APP_ROOT/deploy/beget/.env") == PRESERVED ]]
  python3 - <<'CHECK'
from pathlib import Path
import os
s=Path('/tmp/install-smoke/commands').read_text().splitlines()
a=next(i for i,l in enumerate(s) if l.startswith('build '));b=s.index('BACKUP');c=next(i for i,l in enumerate(s) if ' stop ' in l);d=next(i for i,l in enumerate(s) if ' up -d ' in l)
assert a<b<c<d
max_up=[i for i,l in enumerate(s) if ' up -d ' in l and l.endswith(' maxworker')]
if os.environ['CASE']=='maxon':
 assert len(max_up)==1 and max_up[0]>next(i for i,l in enumerate(s) if ' migrate --check' in l)
else:assert not max_up
CHECK
 elif [[ $CASE == healthfail || $CASE == migrationfail ]]; then
  [[ $status != 0 ]]
  ! grep -q 'Обновление 6.30.0 установлено' /tmp/install-smoke/output
  ! grep -q ' up -d --no-build maxworker' /tmp/install-smoke/commands
 else
  [[ $status != 0 && $(cat "$ARMADA_APP_ROOT/index.html") == OLD ]]
  if [[ -f /tmp/install-smoke/commands ]]; then ! grep -q ' up -d ' /tmp/install-smoke/commands; fi
 fi
 printf 'PASS installer guard: %s\\n' "$CASE"
done
''')
for name in ('docker','curl','unzip','sleep','df','run'):(root/name).chmod(0o755)
env=os.environ.copy()
for key in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):env.pop(key,None)
result=subprocess.run(['docker','--host=unix:///var/run/docker.sock','run','--rm','--network','none','--user','0:0','--mount',f'type=bind,src={root},dst=/fixtures,readonly','--entrypoint','bash','python:3.12-slim-bookworm','/fixtures/run'],env=env)
if result.returncode:print('Installer evidence:',root)
raise SystemExit(result.returncode)
