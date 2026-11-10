"""Offline failure injection. Run as root in an isolated container, never on a VPS."""
import hashlib,json,os,shutil,stat,subprocess,tempfile,unittest,zipfile
from pathlib import Path
REPO=Path(__file__).resolve().parents[1]
VERSION='6.31.0'
DOCKER=r'''#!/usr/local/bin/python3
import fcntl,json,os,sys,tarfile,io
a=sys.argv[1:];root=__import__('pathlib').Path(os.environ['FIXTURE']);p=root/'state.json';s=json.loads(p.read_text());case=os.environ['CASE'];app=__import__('pathlib').Path(os.environ['ARMADA_APP_ROOT'])
s['calls'].append(a)
def save():p.write_text(json.dumps(s))
def probe(label):
 s['probes'].append([label,'web' in s['running']]);save()
if a[0]=='info':pass
elif a[0]=='build':
 if case=='buildfail':save();sys.exit(17)
elif a[0]=='inspect':
 if '{{.Image}}' in a:print('sha256:'+'a'*64)
 elif 'Health.Status' in ' '.join(a):print('unhealthy' if case=='healthfail' and (app/'index.html').read_text()=='NEW' else 'healthy')
 else:print('true' if a[-1] in s['running'] else 'false')
elif a[0]=='compose':
 if 'ps' in a:
  if '--services' in a:print('\n'.join(x for x in s['running'] if x in a))
  elif '-q' in a and a[-1] in s['running']:print(a[-1])
 elif 'exec' in a and 'release_version' in ' '.join(a):print(VERSION if (app/'index.html').read_text()=='NEW' else '6.30.0')
 elif 'config' in a:
  if '--quiet' in a:
   probe('before_migrations')
   with (app/'.armada-update.lock').open('w') as lock:
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:s['same_lock_inode']=True
    else:s['same_lock_inode']=False
   if case=='prefail':save();sys.exit(18)
 elif 'stop' in a:
  s['running']=[x for x in s['running'] if x not in a[a.index('stop')+1:]]
 elif 'start' in a:s['running']+=a[a.index('start')+1:]
 elif 'pg_dump' in a:
  probe('during_backup');sys.stdout.buffer.write(b'SYNTHETIC DUMP')
  if case=='backupfail':save();sys.exit(19)
 elif 'pg_restore' in a:print('SYNTHETIC TOC')
 elif 'tar' in a:
  b=io.BytesIO()
  with tarfile.open(fileobj=b,mode='w'):pass
  sys.stdout.buffer.write(b.getvalue())
 elif 'run' in a and 'migrate' in a and '--noinput' in a:
  probe('migrations');s['migration_attempted']=True
  if case=='migrationfail':save();sys.exit(20)
 elif 'run' in a and 'check' in a and case=='postfail':save();sys.exit(21)
 elif 'up' in a:
  services=[x for x in ('web','mailworker','maxworker','proxy') if x in a]
  s['running']=list(dict.fromkeys(s['running']+services))
  if any(x.endswith('images.json') for x in a):s['rolled_back']=True
  if case=='maxfail' and services==['maxworker']:s['running'].remove('maxworker')
save()
'''.replace('print(VERSION if',"print('6.31.0' if")

class InstallerTests(unittest.TestCase):
 def run_case(self,case,active_max=False):
  with tempfile.TemporaryDirectory(prefix='armada-safety-') as td:
   root=Path(td);app=root/'openIA-feature-accounts-foundation';d=app/'deploy/beget';d.mkdir(parents=True)
   for n in ('backup.sh','compose.yml','Caddyfile'):shutil.copy2(REPO/'deploy/beget'/n,d/n)
   (app/'index.html').write_text('OLD');(app/'obsolete.py').write_text('OLD CODE')
   (d/'.env').write_text('SYNTHETIC=preserve\n');(d/'.env').chmod(0o640)
   (app/'.env').write_text('ROOT_SYNTHETIC=preserve\n');(app/'.env').chmod(0o600)
   original=['web','mailworker']+(['maxworker'] if active_max else [])+['proxy']
   (root/'state.json').write_text(json.dumps({'running':original,'calls':[],'probes':[],'migration_attempted':False}))
   archive=root/'openIA-6.31.0-update.zip';prefix='openIA-feature-accounts-foundation/'
   files={'index.html':'NEW','server/config/settings.py':'','server/accounts/context.py':"def version(): return {'release_version':'6.31.0'}",'deploy/synology/Dockerfile':'FROM scratch'}
   for n in ('backup.sh','compose.yml','Caddyfile','switch_tree.py'):files['deploy/beget/'+n]=(REPO/'deploy/beget'/n).read_text()
   with zipfile.ZipFile(archive,'w') as z:
    for n,data in files.items():z.writestr(prefix+n,data)
   template=(REPO/'deploy/beget/install-update.sh.in').read_text().replace('__VERSION__',VERSION).replace('__ARCHIVE_SHA256__',hashlib.sha256(archive.read_bytes()).hexdigest())
   installer=root/'install.sh';installer.write_text(template)
   bin=root/'bin';bin.mkdir();(bin/'docker').write_text(DOCKER)
   (bin/'curl').write_text('#!/bin/sh\nexit 97\n')
   (bin/'sleep').write_text('#!/bin/sh\nexit 0\n')
   (bin/'df').write_text('#!/bin/sh\nprintf "Filesystem 1024-blocks Used Available Capacity Mounted\\nfixture 99999999 0 99999999 0%% /\\n"\n')
   (bin/'unzip').write_text('''#!/usr/local/bin/python3
import os,sys,zipfile
with zipfile.ZipFile(sys.argv[2]) as z:
 if sys.argv[1]=='-tq':assert z.testzip() is None
 else:
  z.extractall(sys.argv[4])
  for i in z.infolist():os.chmod(os.path.join(sys.argv[4],i.filename),0o755 if i.is_dir() else 0o644)
''')
   for f in bin.iterdir():f.chmod(0o755)
   env=os.environ|{'PATH':str(bin)+':'+os.environ['PATH'],'FIXTURE':td,'CASE':case,'ARMADA_APP_ROOT':str(app),'ARMADA_BACKUP_DIR':str(root/'backups')}
   result=subprocess.run(['bash',str(installer)],env=env,capture_output=True,text=True,timeout=40)
   s=json.loads((root/'state.json').read_text())
   if case in ('success','maxon'):
    self.assertEqual(result.returncode,0,result.stdout+result.stderr+''.join(p.read_text() for p in root.glob('.armada-recovery-*/backup.log')))
    self.assertEqual((app/'index.html').read_text(),'NEW')
    self.assertFalse((app/'obsolete.py').exists())
    self.assertEqual(set(s['running']),set(original));self.assertTrue(s['migration_attempted']);self.assertTrue(s['same_lock_inode'])
   else:
    self.assertNotEqual(result.returncode,0,result.stdout+result.stderr+''.join(p.read_text() for p in root.glob('.armada-recovery-*/backup.log')))
    if case in ('buildfail','prefail','backupfail'):
     self.assertEqual((app/'index.html').read_text(),'OLD');self.assertEqual(set(s['running']),set(original))
     self.assertFalse(s['migration_attempted'])
     if case=='prefail':
      self.assertTrue(s['rolled_back']);self.assertTrue((app/'obsolete.py').exists())
    else:
     self.assertTrue(s['migration_attempted']);self.assertFalse(set(s['running'])&{'web','mailworker','maxworker'},result.stdout+result.stderr+''.join(p.read_text() for p in root.glob('.armada-recovery-*/backup.log')))
     recovery=list(root.glob('.armada-recovery-*'));self.assertEqual(len(recovery),1)
     self.assertTrue((recovery[0]/'RECOVERY.md').is_file());self.assertTrue((recovery[0]/'archive').is_file())
   for label,writable in s['probes']:self.assertFalse(writable,(label,s['calls']))
   self.assertEqual((d/'.env').read_text(),'SYNTHETIC=preserve\n');self.assertEqual(stat.S_IMODE((d/'.env').stat().st_mode),0o640)
   self.assertEqual((app/'.env').read_text(),'ROOT_SYNTHETIC=preserve\n')

 def test_success_without_max(self):self.run_case('success')
 def test_success_with_previously_active_max(self):self.run_case('maxon',True)
 def test_build_failure_does_not_stop_old_version(self):self.run_case('buildfail')
 def test_backup_failure_restarts_original_services(self):self.run_case('backupfail',True)
 def test_before_migrations_rolls_back_exact_images(self):self.run_case('prefail',True)
 def test_migration_failure_leaves_writers_stopped(self):self.run_case('migrationfail',True)
 def test_after_migrations_failure_leaves_writers_stopped(self):self.run_case('postfail',True)
 def test_health_failure_leaves_writers_stopped(self):self.run_case('healthfail',True)
 def test_max_restart_failure_stops_all_writers(self):self.run_case('maxfail',True)

if __name__=='__main__':unittest.main(verbosity=2)
