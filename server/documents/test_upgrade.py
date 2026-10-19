import hashlib
import secrets
import tempfile
from pathlib import Path
from django.contrib.auth.hashers import make_password
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

class DocumentsUpgradePreservation(TransactionTestCase):
    def test_v3_files_versions_decisions_people_and_passwords_survive(self):
        executor=MigrationExecutor(connection);latest=executor.loader.graph.leaf_nodes()
        old_targets=[('accounts','0004_alter_auditevent_action'),('documents','0001_initial'),('finance',None)]
        with tempfile.TemporaryDirectory(prefix='armada-upgrade-file-') as root:
            path=Path(root)/'ab'/('a'*32);path.parent.mkdir();content=b'%PDF-1.7\nsynthetic legacy version\n%%EOF';path.write_bytes(content)
            try:
                executor.migrate(old_targets)
                old=executor.loader.project_state([('accounts','0004_alter_auditevent_action'),('documents','0001_initial'),('projects','0002_company')]).apps
                User=old.get_model('accounts','User');company=old.get_model('projects','LegalEntity').objects.get_or_create(tax_id='8604073687',defaults={'name':'ТЕСТ Компания'})[0]
                hashed=make_password(secrets.token_urlsafe(24))
                leader=User.objects.create(phone='+70000000001',full_name='ТЕСТ руководитель',leader_slot=1,password=hashed,must_change_password=False,session_version=8)
                worker=User.objects.create(phone='+70000000003',full_name='ТЕСТ работник',password=hashed,must_change_password=False,is_active=False)
                doc=old.get_model('documents','Document').objects.create(legal_entity_id=company.pk,title='ТЕСТ старое удостоверение',section='foreman',kind='personal',created_by_id=leader.pk)
                version=old.get_model('documents','DocumentVersion').objects.create(document_id=doc.pk,sequence=1,storage_key='ab/'+'a'*32,original_name='УДОСТОВЕРЕНИЕ.pdf',content_type='application/pdf',size=len(content),sha256=hashlib.sha256(content).hexdigest(),uploaded_by_id=leader.pk,status='revision')
                doc.current_version_id=version.pk;doc.save()
                old.get_model('documents','ApprovalEvent').objects.create(version_id=version.pk,actor_id=leader.pk,decision='revision',reason='ТЕСТ историческое решение')
                old.get_model('documents','DocumentPerson').objects.create(document_id=doc.pk,person_id=worker.pk)
                executor=MigrationExecutor(connection);executor.migrate(latest);new=executor.loader.project_state(latest).apps
                current=new.get_model('documents','Document').objects.get(pk=doc.pk)
                self.assertEqual(current.current_version_id,version.pk);self.assertEqual(current.section,'personnel');self.assertEqual(path.read_bytes(),content)
                current_version=new.get_model('documents','DocumentVersion').objects.get(pk=version.pk)
                self.assertEqual(current_version.sha256,hashlib.sha256(content).hexdigest());self.assertEqual(current_version.name_key,'удостоверение.pdf');self.assertEqual(current_version.status,'revision')
                self.assertEqual(new.get_model('documents','ApprovalEvent').objects.get(version_id=version.pk).reason,'ТЕСТ историческое решение')
                person=new.get_model('documents','Person').objects.get(user_id=worker.pk);self.assertTrue(person.archived);self.assertTrue(current.personnel_linked.filter(pk=person.pk).exists())
                preserved=new.get_model('accounts','User').objects.get(pk=leader.pk);self.assertEqual(preserved.password,hashed);self.assertEqual(preserved.session_version,8)
            finally:MigrationExecutor(connection).migrate(latest)
