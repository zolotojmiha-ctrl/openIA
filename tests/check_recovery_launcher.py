import hashlib,json,os,subprocess,tempfile
from pathlib import Path
source=(Path(__file__).resolve().parents[1]/'deploy/recovery/recover-mac.sh').read_text()
for case in ('remote','bad-hash','good','lowmem','bigarchive','runfail'):
 with tempfile.TemporaryDirectory(prefix='armada-launcher-') as td:
  root=Path(td);project=root/'tools';folder=project/'deploy/recovery';folder.mkdir(parents=True);bin=root/'bin';bin.mkdir()
  archive=root/'archive copy.encrypted';archive.write_bytes(b'SYNTHETIC ARCHIVE')
  if case=='bigarchive':
   with open(archive,'ab') as big:big.truncate(600*1048576)
  key=root/'recovery key.pem';key.write_bytes(b'SYNTHETIC KEY')
  log=root/'calls.jsonl';fake=bin/'fake-docker'
  fake.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
with open(os.environ['CALL_LOG'],'a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
args=sys.argv[1:]
assert args[:2]==['--context','desktop-linux']
if 'context' in args[2:]:print(os.environ['TEST_ENDPOINT'])
elif 'info' in args and '{{.MemTotal}}' in ' '.join(args):print(os.environ.get('TEST_MEM',str(8*1024**3)))
elif 'info' in args:print('Docker Desktop')
elif 'run' in args and os.environ.get('TEST_RUN_EXIT'):sys.exit(int(os.environ['TEST_RUN_EXIT']))
''');fake.chmod(0o755)
  (bin/'uname').write_text('#!/bin/sh\nprintf "Darwin\\n"\n');(bin/'uname').chmod(0o755)
  (bin/'osascript').write_text('''#!/usr/bin/env python3
import os,sys
print(os.environ['TEST_ARCHIVE'] if '.encrypted' in sys.argv[-1] else os.environ['TEST_KEY'])
''');(bin/'osascript').chmod(0o755)
  script=folder/'recover-mac.sh';script.write_text(source.replace('docker_binary=/Applications/Docker.app/Contents/Resources/bin/docker','docker_binary='+str(fake)).replace('receipt_dir="$HOME/Documents/ArmadaRecoveryReceipts"','receipt_dir="'+str(root/'receipts')+'"'))
  env=os.environ|{'PATH':str(bin)+':'+os.environ['PATH'],'TEST_ENDPOINT':'ssh://forbidden.example' if case=='remote' else 'unix:///local/desktop.sock','CALL_LOG':str(log),'TEST_ARCHIVE':str(archive),'TEST_KEY':str(key)}|({'TEST_MEM':str(1024**3)} if case=='lowmem' else {})|({'TEST_RUN_EXIT':'137'} if case=='runfail' else {})
  archive_bytes_after=archive.read_bytes() if case!='bigarchive' else None
  expected='0'*64 if case=='bad-hash' else hashlib.sha256(archive.read_bytes()).hexdigest() if case!='bigarchive' else '1'*64
  r=subprocess.run(['bash',str(script),expected],env=env,capture_output=True,text=True,timeout=15)
  calls=[json.loads(l) for l in log.read_text().splitlines()]
  if case=='runfail':
   assert r.returncode==137 and 'Код выхода контейнера проверки: 137' in r.stderr and 'причина не установлена' in r.stderr and 'SYNTHETIC' not in r.stdout+r.stderr,(r.returncode,r.stdout,r.stderr)
  elif case=='lowmem':assert r.returncode!=0 and 'меньше 1536' in r.stderr and not any('build' in c or 'run' in c for c in calls),(r.stdout,r.stderr)
  elif case=='bigarchive':assert r.returncode!=0 and 'больше расчётного предела' in r.stderr and not any('build' in c or 'run' in c for c in calls),(r.stdout,r.stderr)
  elif case!='good':assert r.returncode!=0 and not any('build' in c or 'run' in c for c in calls),(case,r.stdout,r.stderr)
  else:
   assert r.returncode==0,(r.stdout,r.stderr)
   run=next(c for c in calls if 'run' in c)
   assert run[run.index('--network')+1]=='none' and '--read-only' in run
   for label in ('dst=/inputs/backup.encrypted,readonly','dst=/inputs/recovery.pem,readonly'):assert any(label in arg for arg in run)
   assert '--private-key' in run and '--receipt' in run
   assert archive.read_bytes()==b'SYNTHETIC ARCHIVE' and key.read_bytes()==b'SYNTHETIC KEY'
  print('PASS launcher:',case)
