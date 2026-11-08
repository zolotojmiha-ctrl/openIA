"""Manual isolated PostgreSQL 17 recovery check. Never uses the configured app DB.

Run with the project's Python environment after reading managed Docker/network
requirements. Retains synthetic evidence under .runtime/max-restore-validation.
"""
import json, os, secrets, shutil, subprocess, tempfile, time, uuid
from pathlib import Path
repo=Path(__file__).resolve().parents[1]
evidence=Path(os.environ.get('ARMADA_VALIDATION_DIR', repo/'.runtime/max-restore-validation'))
evidence.mkdir(mode=0o700,parents=True,exist_ok=True)
root=Path(tempfile.mkdtemp(prefix='max-restore-',dir=evidence))
name='armada-max-restore-'+uuid.uuid4().hex[:8]
env=os.environ.copy()
for key in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH','DATABASE_URL_FILE','ARMADA_MAIL_KEY','ARMADA_MAIL_KEY_FILE','ARMADA_MAX_TOKEN','ARMADA_MAX_TOKEN_FILE','ARMADA_MAX_WEBHOOK_SECRET','ARMADA_MAX_WEBHOOK_SECRET_FILE'):
 env.pop(key,None)
env['POSTGRES_PASSWORD']=secrets.token_urlsafe(32)
cli=['docker','--host=unix:///var/run/docker.sock']
def run(args,**kw):
 return subprocess.run(args,check=True,cwd=repo,**kw)
def manage(args,**kw):
 return run([str(repo/'.venv/bin/python'),'server/manage.py',*args],env=env,**kw)
def shell(code):
 manage(['shell','-c',code],stdout=subprocess.DEVNULL)
try:
 run(cli+['run','--detach','--name',name,'--publish','127.0.0.1::5432','--env','POSTGRES_PASSWORD','--tmpfs','/var/lib/postgresql/data:rw,size=512m','postgres:17-bookworm','-c','shared_buffers=32MB','-c','max_wal_size=64MB','-c','min_wal_size=32MB'],env=env,stdout=subprocess.DEVNULL)
 for _ in range(80):
  if subprocess.run(cli+['exec',name,'pg_isready','-U','postgres'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:break
  time.sleep(.25)
 info=json.loads(subprocess.check_output(cli+['inspect',name]))[0]
 port=info['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort']
 env.update(DATABASE_URL=f'postgresql://postgres:{env["POSTGRES_PASSWORD"]}@127.0.0.1:{port}/postgres',ARMADA_DEVELOPMENT='1',ARMADA_MAX_ENABLED='0',ARMADA_RUNTIME_DIR=str(root/'runtime'),PRIVATE_DOCUMENT_ROOT=str(root/'documents'))
 manage(['migrate','--noinput'],stdout=subprocess.DEVNULL)
 manage(['migrate','maxbot','zero','--noinput'],stdout=subprocess.DEVNULL)
 shell('''
from projects import test_stock as f
from repairs.models import Job, Defect, JobDefect, Attachment
from repairs.services import report_work
from documents.storage import store_file, validate_upload
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
import uuid
f.StockTests.setUpTestData.__func__(f.StockTests)
u=f.StockTests.worker
j=Job.objects.create(contract=f.StockTests.contract,section=f.StockTests.section,method='grind',assigned_to=u,created_by=f.StockTests.leader)
d=Defect.objects.create(contract=j.contract,section=j.section,kilometre=1,number='RESTORE-SYNTHETIC')
JobDefect.objects.create(job=j,defect=d)
r=report_work(u,j.pk,'result',{'token':uuid.uuid4(),'expected_version':j.version,'performed_date':timezone.localdate(),'outcome':'partial','method':'grind','note':'SYNTHETIC LEGACY REPORT','couplings':[],'files':[]})
data=validate_upload(SimpleUploadedFile('synthetic.txt',b'SYNTHETIC LEGACY FILE'))
key=store_file(data)
Attachment.objects.create(report=r,storage_key=key,**{k:data[k] for k in ('name','content_type','size','sha256')})
''')
 manage(['migrate','maxbot','--noinput'],stdout=subprocess.DEVNULL)
 shell('''
import io,uuid
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone
from accounts.models import User
from repairs.models import Job,Report
from documents.storage import store_file,validate_upload
from maxbot import links,dialog
from maxbot.models import Draft,Photo,Inbox
u=User.objects.get(phone='+70000000002')
assert Report.objects.count()==1
code=links.issue(u)
with transaction.atomic():links.request_link(links.digest(code),101,201)
b=links.approve(u,links.digest(code))
j=Job.objects.get()
d=dialog.start(b,j,'repaired')
d.raw.update(performed_date=str(timezone.localdate()),note='SYNTHETIC RESTORED DRAFT',coordinates='55, 73')
d.step=len(d.fields);d.state='confirm';d.save()
output=io.BytesIO();Image.new('RGB',(2,2),'white').save(output,format='PNG')
data=validate_upload(SimpleUploadedFile('photo.png',output.getvalue()))
key=store_file(data)
Photo.objects.create(draft=d,storage_key=key,**{k:data[k] for k in ('name','sha256','size')})
Inbox.objects.create(key=uuid.uuid4().hex,max_user=101,chat=201,payload={'kind':'message_callback','text':'confirm:'+str(d.pk),'photos':[],'timestamp':1000})
''')
 with open(root/'database.dump','wb') as output:
  run(cli+['exec',name,'pg_dump','-U','postgres','-d','postgres','--no-owner','--no-acl','-Fc'],stdout=output)
 shutil.copytree(root/'documents',root/'restored-documents')
 (root/'restored-runtime').mkdir(mode=0o700)
 shutil.copy2(root/'runtime'/'secret-key',root/'restored-runtime'/'secret-key')
 run(cli+['exec',name,'createdb','-U','postgres','max_restored'])
 with open(root/'database.dump','rb') as source:
  run(cli+['exec','-i',name,'pg_restore','-U','postgres','-d','max_restored','--exit-on-error','--no-owner','--no-acl'],stdin=source,stdout=subprocess.DEVNULL)
 env['DATABASE_URL']=env['DATABASE_URL'].rsplit('/',1)[0]+'/max_restored'
 env['ARMADA_RUNTIME_DIR']=str(root/'restored-runtime')
 env['PRIVATE_DOCUMENT_ROOT']=str(root/'restored-documents')
 manage(['migrate','--noinput'],stdout=subprocess.DEVNULL)
 shell('''
from django.test import override_settings
from unittest.mock import patch
from accounts.models import User
from repairs.models import Report,Attachment
from documents.storage import verified_file
from maxbot.models import Binding,Draft,Photo,Inbox,Outbox
from maxbot import worker
assert Report.objects.count()==1 and Attachment.objects.count()==1
assert Binding.objects.filter(active=True,max_user=101,chat=201).count()==1
assert Draft.objects.get().state=='confirm'
with verified_file(Attachment.objects.get()) as f:assert f.read()==b'SYNTHETIC LEGACY FILE'
with verified_file(Photo.objects.get()) as f:assert f.read().startswith(bytes.fromhex('89504e47'))
key=Inbox.objects.get(state='pending').pk
with override_settings(ARMADA_MAX_ENABLED=True),patch('maxbot.transport.send',side_effect=AssertionError('NO EXTERNAL SEND')):
 worker.process(key);worker.process(key)
assert Report.objects.count()==2
assert Attachment.objects.count()==2
assert Draft.objects.get().state=='submitted'
assert Inbox.objects.get(pk=key).state=='done'
assert Outbox.objects.filter(state='sent').count()==0
''')
 print('PASS PostgreSQL17 additive upgrade and actual pg_dump/pg_restore: legacy report/file, MAX binding/draft/photo/config restored; pending confirmation processed once; no external send.')
 print('Synthetic evidence directory:',root.name)
finally:
 subprocess.run(cli+['rm','--force','--volumes',name],stdout=subprocess.DEVNULL)
