import os,subprocess,tempfile,shutil,json,sys
from pathlib import Path
repo=Path(__file__).resolve().parents[1];package=Path(sys.argv[1]).resolve();manifest=json.loads((package/'manifest.json').read_text())
with tempfile.TemporaryDirectory() as tmp:
 root=Path(tmp)/'armada';root.mkdir();binpath=Path(tmp)/'bin';binpath.mkdir()
 for name in set(manifest['files'])|set(manifest.get('requires',{}))|{'server/projects/rental_views.py'}:
  if name in manifest['files'] and manifest['files'][name]['before'] is None:continue
  path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(subprocess.check_output(['git','show',manifest['base']+':'+name],cwd=repo))
 deploy=root/'deploy/beget';deploy.mkdir(parents=True);(deploy/'compose.yml').write_text('services: {}');(deploy/'backup.sh').write_text('set -e\ntouch backup-ran\n')
 (root/'existing-data').write_text('TEST unchanged')
 (binpath/'docker').write_text("""#!/bin/sh
printf '%s\n' "$*" >> "$ARMADA_TEST_DOCKER_LOG"
case "$*" in *" mail_trash_status --require-trash") [ "$ARMADA_TEST_STATUS_FAIL" = 1 ] && exit 1;; *" ps --status running -q maxworker") [ "$ARMADA_TEST_MAX" = 1 ] && printf '%s\n' TEST-MAX-CONTAINER;; esac
exit 0
""");(binpath/'docker').chmod(0o755)
 env=os.environ|{'PATH':str(binpath)+':'+os.environ['PATH'],'ARMADA_TEST_DOCKER_LOG':str(Path(tmp)/'docker.log'),'ARMADA_TEST_MAX':'1' if '--expect-max' in sys.argv else '0','ARMADA_APP_ROOT':str(root),'ARMADA_SOURCE_BACKUP_ROOT':str(Path(tmp)/'backups')}
 command=['bash',str(package/'install.sh')]
 target=root/next(name for name,entry in manifest['files'].items() if entry['before'] is not None);original=target.read_bytes();target.write_bytes(original+b'\n# changed\n')
 result=subprocess.run(command,env=env,capture_output=True);assert result.returncode!=0 and not (root/'backup-ran').exists()
 target.write_bytes(original)
 for _ in range(2):
  result=subprocess.run(command,env=env,capture_output=True);assert result.returncode==0,result.stdout+result.stderr
 assert (root/'existing-data').read_text()=='TEST unchanged'
 for name in manifest['files']:assert (root/name).read_bytes()==(package/'files'/name).read_bytes()
 assert len(list((Path(tmp)/'backups').iterdir()))==2
 commands=(Path(tmp)/'docker.log').read_text()
 restarted_max=any('--profile max up' in line and line.endswith(' maxworker') for line in commands.splitlines())
 assert restarted_max==('--expect-max' in sys.argv),commands
 if 'server/manage.py migrate' in commands:
  entries=commands.splitlines()
  build=next(i for i,line in enumerate(entries) if line.endswith(' build web'))
  stop=next(i for i,line in enumerate(entries) if ' stop --timeout 60 ' in line)
  migrate=next(i for i,line in enumerate(entries) if 'server/manage.py migrate' in line)
  up=next(i for i,line in enumerate(entries) if ' up -d ' in line)
  assert build<stop<migrate<up,commands
 print('PASS installer rejects altered source before backup; installs; repeats safely; preserves unrelated data; retains source backups')

 if '--expect-status-check' in sys.argv:
  failed_env=env|{'ARMADA_TEST_STATUS_FAIL':'1'}
  result=subprocess.run(command,env=failed_env,capture_output=True)
  output=(result.stdout+result.stderr).decode()
  assert result.returncode!=0,output
  assert 'Код обновлён, сервисы запущены.' in output,output
  assert 'Возможности корзины подтверждены' not in output,output
  assert (root/'existing-data').read_text()=='TEST unchanged'
  for name in manifest['files']:assert (root/name).read_bytes()==(package/'files'/name).read_bytes()
  print('PASS failed server capability check cannot produce success; updated code and unrelated data remain intact')
