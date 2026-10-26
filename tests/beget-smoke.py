"""Isolated Docker production/backup check; creates and removes ONLY a random test project.
Never points at the live armada project and never starts public proxy or external mail.
Run from a disposable development machine with images armada-web:6.14.2/armada-db:17 built.
"""
import subprocess,os,secrets,time,tempfile,pathlib,tarfile,hashlib,io,json,sys
repo=pathlib.Path(__file__).resolve().parents[1]; project='armada-verify-'+secrets.token_hex(4)
env=os.environ|{'DOCKER_CONFIG':os.environ.get('DOCKER_CONFIG',str(pathlib.Path.home()/'.docker')),'COMPOSE_PROJECT_NAME':project,'TLS_EMAIL':'verify@example.invalid'}
base=['docker','compose','-p',project,'-f',str(repo/'deploy/beget/compose.yml')]
def run(args,**kw):
 try:return subprocess.check_output(args,env=env,stderr=subprocess.STDOUT,**kw)
 except subprocess.CalledProcessError as e:
  print(e.output.decode(errors='replace'),flush=True);raise
def compose(*args,**kw):return run(base+list(args),**kw)
def shell(code):return compose('exec','-T','web','python','server/manage.py','shell','-c',code)
upgrade=next((a.split('=',1)[1] for a in sys.argv if a.startswith('--upgrade-from=')),None)
SEED="from django.conf import settings\nfrom accounts.models import User\nfrom postoffice.models import Mailbox\nfrom postoffice.crypto import seal\nfrom projects.models import LegalEntity,COMPANY_TAX_ID,Contract,Equipment\nfrom documents.models import Person\nfrom pathlib import Path\nassert User.objects.count()==0\nu=User.objects.create_user(phone='+79999999001',password='123456',full_name='Synthetic Restore Check',leader_slot=1)\ncompany=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)\nContract.objects.create(legal_entity=company,number='KEEP-614',subject='Preserve existing customer data')\nPerson.objects.create(legal_entity=company,full_name='Keep worker without login',created_by=u)\nEquipment.objects.create(legal_entity=company,name='Keep equipment',category='Vehicle',inventory_number='KEEP-614',created_by=u)\nMailbox.objects.create(address='restore-check@armada-sibir.ru',owner=u,created_by=u,state='requested',credential=seal('synthetic-backup-secret'))\nPath(settings.PRIVATE_DOCUMENT_ROOT,'restore-proof.txt').write_text('synthetic document restore proof')\nprint('Synthetic fixtures created; no real mail connection.')"
VERIFY="from django.conf import settings\nfrom django.test import Client\nfrom accounts.models import User\nfrom postoffice.models import Mailbox\nfrom postoffice.crypto import reveal\nfrom projects.models import Contract,Equipment,CounterpartyAgreement\nfrom documents.models import Person\nfrom pathlib import Path\nassert not settings.DEBUG\nassert settings.SESSION_COOKIE_SECURE and settings.CSRF_COOKIE_SECURE\nassert Client().get('/cabinet/login/',HTTP_HOST='app.armada-sibir.ru',HTTP_X_FORWARDED_PROTO='https').status_code==200\nu=User.objects.get(phone='+79999999001');assert u.check_password('123456') and u.full_name=='Synthetic Restore Check'\nassert Contract.objects.get().number=='KEEP-614'\nassert Equipment.objects.get().inventory_number=='KEEP-614'\nassert Person.objects.get().user_id is None\nassert reveal(Mailbox.objects.get().credential)=='synthetic-backup-secret'\nassert Path(settings.PRIVATE_DOCUMENT_ROOT,'restore-proof.txt').read_text()=='synthetic document restore proof'\nassert CounterpartyAgreement.objects.count()==0\nprint('Secure settings, existing accounts/passwords, customer contract, worker, equipment, encrypted mailbox and document preserved.')"
try:
 if upgrade:
  with tempfile.NamedTemporaryFile(mode='w',suffix='.yml') as override:
   override.write('services:\n  init:\n    image: '+upgrade+'\n  web:\n    image: '+upgrade+'\n  mailworker:\n    image: '+upgrade+'\n');override.flush()
   print('Starting previous release with synthetic data.',flush=True)
   run(base+['-f',override.name,'up','-d','--no-build','init','db','web','mailworker'])
  print(shell(SEED).decode(),flush=True)
 print('Starting isolated production services '+project,flush=True)
 compose('up','-d','--no-build','init','db','web','mailworker')
 for _ in range(60):
  rows=json.loads(compose('ps','--format','json','web').decode())
  row=rows[0] if isinstance(rows,list) else rows
  if row.get('Health')=='healthy':break
  time.sleep(1)
 else:raise RuntimeError('Production health check failed')
 print('Production web healthy; checking secure settings and synthetic backup data.',flush=True)
 if not upgrade:shell(SEED)
 print(shell(VERIFY).decode(),flush=True)
 with tempfile.TemporaryDirectory(prefix='armada-backup-verify-') as tmp:
  backupenv=env|{'ARMADA_BACKUP_DIR':tmp}
  print(subprocess.check_output(['bash',str(repo/'deploy/beget/backup.sh')],env=backupenv,stderr=subprocess.STDOUT).decode(),flush=True)
  archive=next(pathlib.Path(tmp).glob('*.tar.gz'))
  with tarfile.open(archive) as t:
   payload={m.name.removeprefix('./'):t.extractfile(m).read() for m in t if m.isfile()}
  for line in payload['SHA256SUMS'].decode().splitlines():
   digest,name=line.split();assert hashlib.sha256(payload[name]).hexdigest()==digest
  with tarfile.open(fileobj=io.BytesIO(payload['config.tar'])) as t:
   key=t.getmember('./mail-key');assert key.mode==0o600
  with tarfile.open(fileobj=io.BytesIO(payload['documents.tar'])) as t:
   assert t.extractfile('./restore-proof.txt').read()==b'synthetic document restore proof'
  compose('exec','-T','db','createdb','-U','armada_admin','-O','armada_app','armada_restore')
  compose('exec','-T','db','pg_restore','-U','armada_admin','--role=armada_app','-d','armada_restore','--no-owner','--no-acl',input=payload['database.dump'])
  print(shell('''from django.db import connections
from accounts.models import User
from postoffice.models import Mailbox
from postoffice.crypto import reveal
connections['default'].close()
connections['default'].settings_dict['NAME']='armada_restore'
u=User.objects.get(phone='+79999999001')
assert u.check_password('123456')
from projects.models import Contract,Equipment
from documents.models import Person
assert Contract.objects.get().number=='KEEP-614'
assert Equipment.objects.get().inventory_number=='KEEP-614'
assert Person.objects.get().user_id is None
assert reveal(Mailbox.objects.get(owner=u).credential)=='synthetic-backup-secret'
print('Database, password, encrypted mailbox and document backup restore verified.')''').decode(),flush=True)
 print('PRODUCTION SMOKE AND BACKUP RESTORE OK',flush=True)
except Exception:
 print(compose('logs','--tail=60','db','web','init').decode(),flush=True)
 raise
finally:
 print(compose('down','-v','--remove-orphans').decode(),flush=True)
