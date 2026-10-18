import secrets
from datetime import timedelta
from django.contrib.auth.hashers import make_password
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

class UpgradePreservationTest(TransactionTestCase):
    def test_accounts_passwords_grants_sessions_survive_upgrade(self):
        executor=MigrationExecutor(connection)
        latest=executor.loader.graph.leaf_nodes()
        before=[('accounts','0002_user_temporary_expires_at'),('projects',None)]
        try:
            executor.migrate(before)
            old=executor.loader.project_state([('accounts','0002_user_temporary_expires_at'),('sessions','0001_initial')]).apps
            User=old.get_model('accounts','User')
            hashed=make_password(secrets.token_urlsafe(24))
            leader=User.objects.create(phone='+70000000001',full_name='ТЕСТ старый руководитель',leader_slot=1,password=hashed,must_change_password=False,session_version=7)
            employee=User.objects.create(phone='+70000000003',full_name='ТЕСТ старый сотрудник',password=hashed,must_change_password=False,session_version=4)
            old.get_model('accounts','AccessGrant').objects.create(user_id=employee.pk,permission='document.read',granted_by_id=leader.pk)
            old.get_model('accounts','AuditEvent').objects.create(actor_id=leader.pk,subject_id=employee.pk,action='employee_created')
            old.get_model('sessions','Session').objects.create(session_key='test-migration-session',session_data='test-signed-placeholder',expire_date=timezone.now()+timedelta(hours=1))
            executor=MigrationExecutor(connection)
            executor.migrate(latest)
            new=executor.loader.project_state(latest).apps
            restored=new.get_model('accounts','User').objects.get(pk=leader.pk)
            self.assertEqual(restored.password,hashed)
            self.assertEqual(restored.session_version,7)
            self.assertFalse(restored.must_change_password)
            self.assertEqual(new.get_model('accounts','AccessGrant').objects.get(user_id=employee.pk).permission,'document.read')
            self.assertEqual(new.get_model('accounts','AuditEvent').objects.count(),1)
            self.assertEqual(new.get_model('sessions','Session').objects.get(session_key='test-migration-session').session_data,'test-signed-placeholder')
            self.assertEqual(new.get_model('projects','ProjectObject').objects.count(),0)
            self.assertEqual(new.get_model('projects','Contract').objects.count(),0)
            self.assertTrue(new.get_model('projects','LegalEntity').objects.filter(tax_id='8604073687').exists())
        finally:
            MigrationExecutor(connection).migrate(latest)
