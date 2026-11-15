"""Offline failure injection. Run as root in an isolated container, never on a VPS."""
import fcntl,hashlib,json,os,shutil,stat,subprocess,tempfile,unittest,zipfile
from pathlib import Path
REPO=Path(__file__).resolve().parents[1]
VERSION='6.33.1'
DOCKER=r'''#!/usr/local/bin/python3
import fcntl,json,os,sys,tarfile,io
a=sys.argv[1:];root=__import__('pathlib').Path(os.environ['FIXTURE']);p=root/'state.json';s=json.loads(p.read_text());case=os.environ['CASE'];app=__import__('pathlib').Path(os.environ['ARMADA_APP_ROOT'])
s['calls'].append(a)
def save():p.write_text(json.dumps(s))
def probe(label):
 s['probes'].append([label,'web' in s['running']]);save()
if a[0]=='info':
 if '--format' in a:print(str(root/'docker-root'))
elif a[0]=='image':
 if 'inspect' in a:print(os.environ.get('IMG_SIZE','1000000'))
elif a[0]=='build':
 if case=='buildfail':save();sys.exit(17)
elif a[0]=='inspect':
 if '{{.Image}}' in a:print('sha256:'+'a'*64)
 elif 'Health.Status' in ' '.join(a):print('unhealthy' if case=='healthfail' and (app/'index.html').read_text()=='NEW' else 'healthy')
 else:print('true' if a[-1] in s['running'] else 'false')
elif a[0]=='compose':
 if 'ps' in a:
  if '--services' in a:
   if case=='legacy' and 'maxworker' in a:save();sys.exit(91)
   print('\n'.join(x for x in s['running'] if x in a))
  elif '-q' in a and a[-1] in s['running']:print(a[-1])
 elif 'exec' in a and 'release_version' in ' '.join(a):print('6.24.0' if case=='unsupported' else '6.33.1' if (app/'index.html').read_text()=='NEW' and case!='versionfail' else '6.33.0')
 elif 'config' in a:
  if '--services' in a:print('web\nmailworker\nproxy' if case=='legacy' else 'web\nmailworker\nmaxworker\nproxy')
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
 elif 'du' in a and 'run' in a:print('1000\t/run/armada');print('1000\t/var/lib/armada-documents')
 elif 'pg_dump' in a:
  probe('during_backup');sys.stdout.buffer.write(b'SYNTHETIC DUMP')
  if case=='backupfail':save();sys.exit(19)
 elif 'psql' in a and 'pg_database_size(current_database())' in ' '.join(a):print('abc' if case=='measurefail' else os.environ.get('DB_BYTES','1000000'))
 elif 'psql' in a:print('{}')
 elif 'pg_restore' in a:print('SYNTHETIC TOC')
 elif 'tar' in a:
  b=io.BytesIO()
  with tarfile.open(fileobj=b,mode='w'):pass
  sys.stdout.buffer.write(b.getvalue())
 elif 'run' in a and 'migrate' in a and '--noinput' in a:
  probe('migrations');s['migration_attempted']=True
  if case=='migrationfail':save();sys.exit(20)
 elif 'run' in a and '--check' in a and 'migrate' in a and case=='schemafail':save();sys.exit(22)
 elif 'run' in a and 'check' in a and case=='postfail':save();sys.exit(21)
 elif 'up' in a:
  services=[x for x in ('web','mailworker','maxworker','proxy') if x in a]
  s['running']=list(dict.fromkeys(s['running']+services))
  if any(x.endswith('images.json') for x in a):s['rolled_back']=True
  if case=='maxfail' and services==['maxworker']:s['running'].remove('maxworker')
save()
'''.replace('print(VERSION if',"print('6.33.1' if")

DF=r'''#!/usr/local/bin/python3
import json,os,sys
path=sys.argv[-1];spec=json.loads(os.environ.get('DF_SPEC','{}'))
mount,avail=spec.get(path,['/',999999999])
print('Filesystem 1024-blocks Used Available Capacity Mounted on');print('fixture 1000 900 %d 90%% %s'%(avail,mount))
'''

class InstallerTests(unittest.TestCase):
 def run_case(self,case,active_max=False,args=('--allow-unencrypted-backup',),twice=False,inspect=None,extra=None,df=None,env_extra=None,big_mib=0,backup_sub=False):
  with tempfile.TemporaryDirectory(prefix='armada-safety-') as td:
   root=Path(td);app=root/('armada' if case in ('directroot','directrelative') else 'unexpected-root' if case=='badroot' else 'openIA-feature-accounts-foundation');d=app/'deploy/beget';d.mkdir(parents=True)
   for n in ('backup.sh','compose.yml','Caddyfile'):shutil.copy2(REPO/'deploy/beget'/n,d/n)
   (app/'index.html').write_text('OLD');(app/'obsolete.py').write_text('OLD CODE')
   (root/'docker-root').mkdir();(root/'vol-b').mkdir()
   if extra:extra(app)
   (d/'.env').write_text('SYNTHETIC=preserve\n');(d/'.env').chmod(0o640)
   (app/'.env').write_text('ROOT_SYNTHETIC=preserve\n');(app/'.env').chmod(0o600)
   original=['web','mailworker']+(['maxworker'] if active_max else [])+['proxy']
   (root/'state.json').write_text(json.dumps({'running':original,'calls':[],'probes':[],'migration_attempted':False}))
   archive=root/'openIA-6.33.1-update.zip';prefix='openIA-feature-accounts-foundation/'
   files={'index.html':'NEW','server/config/settings.py':'','server/accounts/context.py':"def version(): return {'release_version':'6.33.1'}",'deploy/synology/Dockerfile':'FROM scratch'}
   for n in ('backup.sh','compose.yml','Caddyfile','switch_tree.py','backup_counts.sql','backup_manifest.py','backup_retention.py','backup_status.py'):files['deploy/beget/'+n]=(REPO/'deploy/beget'/n).read_text()
   files['deploy/beget/known-release-paths.txt']='obsolete.py\n'
   files['server/projects/disk_crypto.py']=(REPO/'server/projects/disk_crypto.py').read_text()
   if big_mib:files['server/big.bin']='\0'*(big_mib*1048576)
   if case=='wrongversion':files['server/accounts/context.py']="def version(): return {'release_version':'6.29.0'}"
   with zipfile.ZipFile(archive,'w') as z:
    for n,data in files.items():z.writestr(prefix+n,data)
   template=(REPO/'deploy/beget/install-update.sh.in').read_text().replace('__VERSION__',VERSION).replace('__ARCHIVE_SHA256__',hashlib.sha256(archive.read_bytes()).hexdigest())
   if case in ('traversal','symlink','duplicate'):
    with zipfile.ZipFile(archive,'a') as z:
     if case=='traversal':z.writestr(prefix+'../../escape','BAD')
     elif case=='symlink':
      i=zipfile.ZipInfo(prefix+'unsafe');i.create_system=3;i.external_attr=(stat.S_IFLNK|0o777)<<16;z.writestr(i,'/tmp/escape')
     elif case=='duplicate':z.writestr(prefix+'index.html','BAD')
     else:z.writestr(prefix+'server/accounts/context.py',"def version(): return {'release_version':'6.29.0'}")
    template=(REPO/'deploy/beget/install-update.sh.in').read_text().replace('__VERSION__',VERSION).replace('__ARCHIVE_SHA256__',hashlib.sha256(archive.read_bytes()).hexdigest())
   installer=root/'install.sh';installer.write_text(template)
   key=root/'synthetic-signing.key';public=root/'trusted-public.pem'
   def openssl(*args):subprocess.run(['openssl',*map(str,args)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
   openssl('genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:3072','-out',key)
   key.chmod(0o600);openssl('pkey','-in',key,'-pubout','-out',public);public.chmod(0o644)
   meta=root/'openIA-6.33.1-update.json';meta.write_text(json.dumps({'version':VERSION,'ready_for_install':case!='notvalidated','source_commit':'a'*40}))
   manifest=root/'SHA256SUMS-6.33.1'
   manifest.write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+name+'\n' for p,name in ((archive,archive.name),(installer,'install-6.33.1.sh'),(meta,meta.name))))
   openssl('dgst','-sha256','-sign',key,'-out',str(manifest)+'.sig',manifest)
   if case=='badmanifest':manifest.write_text(manifest.read_text()+'tampered\n')
   if case=='badsignature':Path(str(manifest)+'.sig').write_bytes(b'INVALID')
   if case=='tampered_installer':installer.write_text(installer.read_text()+'\n# modified after signing\n')
   if case=='tamperedarchive':archive.write_bytes(archive.read_bytes()+b'TAMPERED')
   bin=root/'bin';bin.mkdir();(bin/'docker').write_text(DOCKER)
   (bin/'curl').write_text('#!/bin/sh\nexit 97\n')
   (bin/'sleep').write_text('#!/bin/sh\nexit 0\n')
   (bin/'df').write_text(DF)
   (bin/'unzip').write_text('''#!/usr/local/bin/python3
import os,sys,zipfile
with zipfile.ZipFile(sys.argv[2]) as z:
 if sys.argv[1]=='-tq':assert z.testzip() is None
 else:
  z.extractall(sys.argv[4])
  for i in z.infolist():os.chmod(os.path.join(sys.argv[4],i.filename),0o755 if i.is_dir() else 0o644)
''')
   for f in bin.iterdir():f.chmod(0o755)
   env=os.environ|(env_extra(root) if env_extra else {})|{'PATH':str(bin)+':'+os.environ['PATH'],'FIXTURE':td,'CASE':case,'ARMADA_APP_ROOT':str(app),'ARMADA_BACKUP_DIR':str(root/'vol-b'/'backups' if backup_sub else root/'backups'),'DF_SPEC':json.dumps(df(root) if df else {}),'ARMADA_RELEASE_PUBLIC_KEY':str(root/'missing' if case=='missingtrust' else public)}
   lock=None
   if case in ('updatelock','backuplock','diskworkerlock'):
    backups=root/'backups';backups.mkdir(exist_ok=True)
    lock_path=app/'.armada-update.lock' if case=='updatelock' else backups/('.backup.lock' if case=='backuplock' else '.disk-worker.lock')
    lock=lock_path.open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   relative=case in ('relative','directrelative')
   cmd=['bash',installer.name if relative else str(installer),*args]
   result=subprocess.run(cmd,cwd=root if relative else None,env=env,capture_output=True,text=True,timeout=60)
   if twice and result.returncode==0:
    result=subprocess.run(cmd,cwd=root if relative else None,env=env,capture_output=True,text=True,timeout=60)
   if lock:lock.close()
   s=json.loads((root/'state.json').read_text())
   if inspect:
    inspect(self,result,s,root,app,original);return
   if case in ('success','maxon','legacy','directroot','relative','directrelative'):
    self.assertEqual(result.returncode,0,result.stdout+result.stderr+''.join(p.read_text() for p in root.glob('.armada-recovery-*/backup.log')))
    self.assertEqual((app/'index.html').read_text(),'NEW')
    self.assertFalse((app/'obsolete.py').exists())
    self.assertEqual(set(s['running']),set(original));self.assertTrue(s['migration_attempted']);self.assertTrue(s['same_lock_inode'])
   else:
    self.assertNotEqual(result.returncode,0,result.stdout+result.stderr+''.join(p.read_text() for p in root.glob('.armada-recovery-*/backup.log')))
    if case in ('buildfail','prefail','backupfail','badmanifest','badsignature','tamperedarchive','missingtrust','notvalidated','traversal','symlink','duplicate','wrongversion','lowspace','updatelock','backuplock','diskworkerlock','unsupported','badroot'):
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

 def test_rejects_traversal(self):self.run_case('traversal')
 def test_rejects_symlink(self):self.run_case('symlink')
 def test_rejects_duplicate_archive_member(self):self.run_case('duplicate')
 def test_rejects_wrong_version(self):self.run_case('wrongversion')
 def test_rejects_unsupported_running_version(self):self.run_case('unsupported')
 def test_rejects_low_space(self):self.run_case('lowspace',df=lambda r:{'/tmp':['/tmp-vol',100]})
 def test_rejects_parallel_update(self):self.run_case('updatelock')
 def test_rejects_parallel_backup(self):self.run_case('backuplock')
 def test_rejects_active_disk_worker(self):self.run_case('diskworkerlock')
 def test_rejects_modified_signed_manifest(self):self.run_case('badmanifest')
 def test_rejects_invalid_signature(self):self.run_case('badsignature')
 def test_rejects_tampered_archive(self):self.run_case('tamperedarchive')
 def test_rejects_missing_independent_trust_key(self):self.run_case('missingtrust')
 def test_rejects_signed_but_not_validated_release(self):self.run_case('notvalidated')
 def test_legacy_compose_without_max_service(self):self.run_case('legacy')
 def test_success_without_max(self):self.run_case('success')
 def test_direct_armada_installation_directory(self):self.run_case('directroot',True)
 def test_relative_installer_invocation(self):self.run_case('relative')
 def test_relative_invocation_for_direct_armada_directory(self):self.run_case('directrelative',True)
 def test_rejects_unexpected_installation_directory(self):self.run_case('badroot')
 def test_success_with_previously_active_max(self):self.run_case('maxon',True)
 def test_build_failure_does_not_stop_old_version(self):self.run_case('buildfail')
 def test_backup_failure_restarts_original_services(self):self.run_case('backupfail',True)
 def test_before_migrations_rolls_back_exact_images(self):self.run_case('prefail',True)
 def test_migration_failure_leaves_writers_stopped(self):self.run_case('migrationfail',True)
 def test_after_migrations_failure_leaves_writers_stopped(self):self.run_case('postfail',True)
 def test_health_failure_leaves_writers_stopped(self):self.run_case('healthfail',True)
 def test_max_restart_failure_stops_all_writers(self):self.run_case('maxfail',True)


def no_side_effects(test,result,s,app,original,text=None):
    """Refusal before the writer hold: no stop/dump call, old tree and services intact."""
    test.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
    test.assertEqual((app/'index.html').read_text(),'OLD');test.assertEqual(set(s['running']),set(original))
    test.assertFalse(s['migration_attempted'])
    joined=[' '.join(c) for c in s['calls']]
    test.assertFalse([c for c in joined if ' stop' in c or 'pg_dump' in c or c.startswith('build')],joined)
    if text:test.assertIn(text,result.stderr)


class SpaceTests(unittest.TestCase):
 run_case=InstallerTests.run_case
 """Peak free-space estimate per filesystem. Each case is refused before any service is stopped."""
 def short(self,name,df,backup_sub=True,**kw):
  self.run_case(name,True,df=df,backup_sub=backup_sub,inspect=lambda t,r,s,root,app,o:(no_side_effects(t,r,s,app,o,'Недостаточно места'),t.assertIn('нужно не менее',r.stderr)),**kw)
 def test_temp_volume_short(self):self.short('space_tmp',lambda r:{'/tmp':['/tmp-vol',100]})
 def test_backup_volume_short(self):self.short('space_backup',lambda r:{str(r/'vol-b'):['/vol-b',100]})
 def test_app_and_recovery_volume_short(self):self.short('space_app',lambda r:{str(r):['/vol-app',100]})
 def test_docker_volume_short(self):self.short('space_docker',lambda r:{str(r/'docker-root'):['/vol-docker',100]})
 def test_shared_filesystem_sums_costs(self):
  # Each role fits alone (backup 1.5 GiB, app 0.7 GiB) but the shared volume must carry the sum.
  gib=1024*1024
  self.short('space_shared',lambda r:{str(r/'vol-b'):['/shared',int(1.6*gib)],str(r):['/shared',int(1.6*gib)]},env_extra=lambda r:{'DB_BYTES':str(400*1048576)},big_mib=150)
 def test_measurement_error_refuses(self):
  self.run_case('measurefail',True,inspect=lambda t,r,s,root,app,o:no_side_effects(t,r,s,app,o,'Не удалось измерить'))
 def test_sufficient_space_and_separate_volumes_pass(self):
  def ok(t,r,s,root,app,o):
   t.assertEqual(r.returncode,0,r.stdout+r.stderr);t.assertIn('нужно не менее',r.stdout);t.assertEqual((app/'index.html').read_text(),'NEW')
  self.run_case('space_ok',True,df=lambda r:{'/tmp':['/tmp-vol',9999999],str(r/'vol-b'):['/vol-b',9999999],str(r):['/vol-app',9999999],str(r/'docker-root'):['/vol-docker',9999999]},backup_sub=True,inspect=ok)


class LocalFileTests(unittest.TestCase):
 run_case=InstallerTests.run_case
 def refused(self,name,make,path):
  def check(t,r,s,root,app,o):
   no_side_effects(t,r,s,app,o,path);t.assertIn('--allow-local-files',r.stderr)
   t.assertNotIn('SECRET-OVERRIDE-CONTENT',r.stdout+r.stderr)
  self.run_case(name,True,extra=make,inspect=check)
 def test_override_file_refused_without_flag(self):self.refused('local_override',lambda app:(app/'docker-compose.override.yml').write_text('SECRET-OVERRIDE-CONTENT'),'docker-compose.override.yml')
 def test_custom_script_refused(self):self.refused('local_script',lambda app:(app/'deploy/beget/custom.sh').write_text('SECRET-OVERRIDE-CONTENT'),'deploy/beget/custom.sh')
 def test_extra_symlink_refused(self):self.refused('local_link',lambda app:(app/'link-to-somewhere').symlink_to('/etc'),'link-to-somewhere')
 def test_dotenv_symlink_is_a_hard_error(self):
  def make(app):(app/'real.env').write_text('x');(app/'.env.local').symlink_to(app/'real.env')
  def check(t,r,s,root,app,o):
   no_side_effects(t,r,s,app,o);t.assertIn('.env.local',r.stderr);t.assertNotEqual(r.returncode,0)
  self.run_case('local_dotenv_link',True,extra=make,args=('--allow-unencrypted-backup','--allow-local-files'),inspect=check)
 def test_allowed_local_file_is_kept_only_in_recovery(self):
  def check(t,r,s,root,app,o):
   t.assertEqual(r.returncode,0,r.stdout+r.stderr)
   rec=list(root.glob('.armada-recovery-*'))[0]
   t.assertIn('docker-compose.override.yml',(rec/'local-files.txt').read_text())
   t.assertEqual((rec/'tree/docker-compose.override.yml').read_text(),'LOCAL')
   t.assertFalse((app/'docker-compose.override.yml').exists())
   t.assertIn('не применяются автоматически',r.stderr)
  self.run_case('local_allowed',True,extra=lambda app:(app/'docker-compose.override.yml').write_text('LOCAL'),args=('--allow-unencrypted-backup','--allow-local-files'),inspect=check)
 def test_no_extra_files_and_reinstall(self):
  def check(t,r,s,root,app,o):
   t.assertEqual(r.returncode,0,r.stdout+r.stderr);t.assertNotIn('есть файлы, которых нет',r.stderr);t.assertEqual(len(list(root.glob('.armada-recovery-*'))),2)
  self.run_case('local_none',True,twice=True,inspect=check)


class PhaseAndRecoveryTests(unittest.TestCase):
 run_case=InstallerTests.run_case
 def phases(self,root):
  return [l.split(' ')[-1] for l in (list(root.glob('.armada-recovery-*'))[0]/'phases.log').read_text().splitlines()]
 def failed(self,case,expected_phase,path_a):
  def check(t,r,s,root,app,o):
   rec=list(root.glob('.armada-recovery-*'));t.assertEqual(len(rec),1);rec=rec[0]
   t.assertNotEqual(r.returncode,0)
   t.assertEqual((rec/'phase').read_text().strip(),expected_phase)
   t.assertFalse(set(s['running'])&{'web','mailworker','maxworker'},'writers must stay stopped')
   md=(rec/'RECOVERY.md').read_text()
   t.assertIn('Состояние при сбое: '+expected_phase,md);t.assertIn('Путь Б',md)
   if path_a:t.assertIn('Допустимы оба пути',md)
   else:t.assertIn('Путь А запрещён',md)
   t.assertIn('config --quiet',md);t.assertIn('migrate --check',md);t.assertIn('version-check.py',md)
   t.assertTrue((rec/'failure.txt').is_file());t.assertNotIn('SYNTHETIC=preserve',md+(rec/'failure.txt').read_text()+r.stdout+r.stderr)
   t.assertIn(str(rec),r.stderr);t.assertIn('закрытые настройки',r.stderr)
  self.run_case(case,True,inspect=check)
 def test_failure_during_migrations(self):self.failed('migrationfail','migrate_started',False)
 def test_failure_after_migrations_check(self):self.failed('postfail','migrate_done',True)
 def test_schema_check_failure(self):self.failed('schemafail','migrate_done',True)
 def test_version_failure(self):self.failed('versionfail','verify',True)
 def test_health_failure_phase(self):self.failed('healthfail','verify',True)
 def test_phase_order_and_recovery_safety_after_success(self):
  def check(t,r,s,root,app,o):
   t.assertEqual(r.returncode,0,r.stdout+r.stderr)
   t.assertEqual(self.phases(root),['prepare','backup_done','switched','migrate_started','migrate_done','verify','complete'])
   rec=list(root.glob('.armada-recovery-*'))[0];st=rec.stat()
   t.assertEqual(stat.S_IMODE(st.st_mode),0o700);t.assertEqual(st.st_uid,0)
   t.assertIn(str(rec),r.stdout);t.assertIn('закрытые настройки',r.stdout)
   t.assertNotIn('SYNTHETIC=preserve',r.stdout+r.stderr+(rec/'RECOVERY.md').read_text()+(rec/'phases.log').read_text())
   t.assertTrue((rec/'tree/deploy/beget/.env').is_file())
  self.run_case('phases_ok',True,inspect=check)
 def test_unsafe_recovery_parent_refused_without_side_effects(self):
  def make(root):pass
  def check(t,r,s,root,app,o):
   no_side_effects(t,r,s,app,o,'доступен на запись посторонним');t.assertEqual(list(root.glob('.armada-recovery-*')),[])
  def prep(t,r,s,root,app,o):check(t,r,s,root,app,o)
  self.run_case('unsafe_parent',True,env_extra=lambda root:(os.chmod(root,0o777) or {}),inspect=prep)
 def test_recovery_inventory_never_deletes_and_keeps_last_complete(self):
  import importlib.util
  spec=importlib.util.spec_from_file_location('ri',REPO/'deploy/beget/recovery_inventory.py');ri=importlib.util.module_from_spec(spec);spec.loader.exec_module(ri)
  with tempfile.TemporaryDirectory() as td:
   base=Path(td)
   for i,(name,phase,fail) in enumerate((('a','complete',False),('b','complete',False),('c','migrate_started',True),('d','backup_done',False))):
    d=base/('.armada-recovery-6.33.1.'+name);d.mkdir();(d/'phase').write_text(phase+'\n');os.utime(d,(1000+i*10,1000+i*10))
    if fail:(d/'failure.txt').write_text('x')
   rows={r['name'][-1]:r for r in ri.inventory(base)}
   self.assertTrue(rows['a']['manual_cleanup_candidate']);self.assertFalse(rows['b']['manual_cleanup_candidate'])
   self.assertFalse(rows['c']['manual_cleanup_candidate']);self.assertFalse(rows['d']['manual_cleanup_candidate'])
   self.assertEqual(len(list(base.iterdir())),4)


class BackupDefaultsTests(unittest.TestCase):
 run_case=InstallerTests.run_case
 def test_unencrypted_requires_explicit_flag(self):
  self.run_case('noflag',True,args=(),inspect=lambda t,r,s,root,app,o:(no_side_effects(t,r,s,app,o,'--allow-unencrypted-backup'),t.assertFalse((root/'backups').exists())))
 def test_missing_cryptography_names_selected_python(self):
  def env(root):
   fake=root/'fakepython';fake.write_text('#!/bin/sh\nexit 1\n');fake.chmod(0o755)
   key=root/'backup-public.pem';key.write_text('KEY')
   return {'ARMADA_BACKUP_PYTHON':str(fake),'ARMADA_BACKUP_PUBLIC_KEY':str(key)}
  self.run_case('nocrypto',True,args=(),env_extra=env,inspect=lambda t,r,s,root,app,o:(no_side_effects(t,r,s,app,o,'нет модуля cryptography'),t.assertIn('fakepython',r.stderr),t.assertIn('НЕ создаётся',r.stderr)))
 def test_encryption_failure_never_falls_back_to_plaintext(self):
  def env(root):
   key=root/'backup-public.pem';key.write_text('NOT A KEY');return {'ARMADA_BACKUP_PUBLIC_KEY':str(key)}
  def check(t,r,s,root,app,o):
   t.assertNotEqual(r.returncode,0);t.assertIn('шифрование копии не удалось',r.stderr+open(list(root.glob('.armada-recovery-*'))[0]/'backup.log').read())
   t.assertEqual([p.name for p in (root/'backups').iterdir() if p.name.startswith('armada-')],[],'no archive of any kind may remain')
   t.assertEqual((app/'index.html').read_text(),'OLD')
  self.run_case('encfail',True,args=(),env_extra=env,inspect=check)
 def test_encrypted_backup_succeeds_and_status_is_machine_readable(self):
  def env(root):
   key=root/'backup.key';public=root/'backup-public.pem'
   subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:3072','-out',str(key)],check=True,capture_output=True)
   subprocess.run(['openssl','pkey','-in',str(key),'-pubout','-out',str(public)],check=True,capture_output=True)
   return {'ARMADA_BACKUP_PUBLIC_KEY':str(public)}
  def check(t,r,s,root,app,o):
   t.assertEqual(r.returncode,0,r.stdout+r.stderr);t.assertNotIn('БЕЗ шифрования',r.stdout+r.stderr)
   names=[p.name for p in (root/'backups').iterdir()]
   t.assertTrue(any(n.endswith('.encrypted') for n in names));t.assertFalse(any(n.endswith('.tar.gz') or n.endswith('.partial') for n in names),names)
   st=json.loads((root/'backups/backup-status.json').read_text());t.assertTrue(st['last_backup_encrypted']);t.assertEqual(st['warnings'],[])
  self.run_case('encok',True,args=(),env_extra=env,inspect=check)
 def test_plain_backup_status_warns(self):
  def check(t,r,s,root,app,o):
   st=json.loads((root/'backups/backup-status.json').read_text());t.assertEqual([w['code'] for w in st['warnings']],['unencrypted_backup']);t.assertIn('Asia/Omsk',st['timezone_user'])
  self.run_case('plainstatus',True,inspect=check)


class TrustTests(unittest.TestCase):
 run_case=InstallerTests.run_case
 def test_fingerprint_command_matches_independent_computation(self):
  with tempfile.TemporaryDirectory() as td:
   td=Path(td);key=td/'k.pem';pub=td/'pub.pem'
   subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:3072','-out',str(key)],check=True,capture_output=True)
   subprocess.run(['openssl','pkey','-in',str(key),'-pubout','-out',str(pub)],check=True,capture_output=True);pub.chmod(0o644)
   installer=td/'install.sh';installer.write_text((REPO/'deploy/beget/install-update.sh.in').read_text().replace('__VERSION__',VERSION).replace('__ARCHIVE_SHA256__','0'*64))
   result=subprocess.run(['bash',str(installer),'--print-trust-fingerprint'],env=os.environ|{'ARMADA_RELEASE_PUBLIC_KEY':str(pub)},capture_output=True,text=True)
   self.assertEqual(result.returncode,0,result.stderr)
   der=subprocess.run(['openssl','pkey','-pubin','-in',str(pub),'-outform','DER'],capture_output=True,check=True).stdout
   expected=hashlib.sha256(der).hexdigest();self.assertIn(expected,result.stdout)
   signer=subprocess.run(['openssl','pkey','-in',str(key),'-pubout','-outform','DER'],capture_output=True,check=True).stdout
   self.assertEqual(hashlib.sha256(signer).hexdigest(),expected,'same value sign_release.py records as signing_public_key_sha256')
 def test_modified_installer_with_still_valid_manifest_is_refused_in_isolation(self):
  self.run_case('tampered_installer',True,inspect=lambda t,r,s,root,app,o:no_side_effects(t,r,s,app,o))
 def test_valid_signature_does_not_make_modified_installer_trusted(self):
  # The signature covers the manifest, not the code that is about to run. A replaced installer
  # executes whatever it contains, so the owner must verify the installer BEFORE the first run.
  with tempfile.TemporaryDirectory() as td:
   td=Path(td);key=td/'k.pem';pub=td/'pub.pem'
   subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:3072','-out',str(key)],check=True,capture_output=True)
   subprocess.run(['openssl','pkey','-in',str(key),'-pubout','-out',str(pub)],check=True,capture_output=True)
   installer=td/'install-6.33.1.sh';installer.write_text('#!/usr/bin/env bash\necho original\n')
   manifest=td/'SHA256SUMS-6.33.1';manifest.write_text(hashlib.sha256(installer.read_bytes()).hexdigest()+'  '+installer.name+'\n')
   subprocess.run(['openssl','dgst','-sha256','-sign',str(key),'-out',str(manifest)+'.sig',str(manifest)],check=True)
   marker=td/'marker';installer.write_text('#!/usr/bin/env bash\ntouch '+str(marker)+'\n')
   verify=lambda:subprocess.run(['openssl','dgst','-sha256','-verify',str(pub),'-signature',str(manifest)+'.sig',str(manifest)],capture_output=True).returncode
   self.assertEqual(verify(),0,'manifest signature itself is still valid')
   listed=manifest.read_text().split()[0]
   self.assertNotEqual(hashlib.sha256(installer.read_bytes()).hexdigest(),listed,'owner-side check before first run catches the replacement')
   subprocess.run(['bash',str(installer)],check=True);self.assertTrue(marker.exists(),'a replaced script runs regardless of the manifest: only the pre-run check protects')


if __name__=='__main__':unittest.main(verbosity=2)
