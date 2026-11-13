from unittest import skipUnless
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

@skipUnless(connection.vendor=='postgresql','Migration preservation check on PostgreSQL')
class WatchMigrationTests(TransactionTestCase):
    def test_old_watch_is_retained_and_only_comparison_format_gets_baseline_marker(self):
        executor=MigrationExecutor(connection)
        executor.migrate([('maxbot','0005_demandwatch')])
        try:
            apps=executor.loader.project_state([('maxbot','0005_demandwatch')]).apps
            User=apps.get_model('accounts','User');Binding=apps.get_model('maxbot','Binding');Watch=apps.get_model('maxbot','DemandWatch')
            user=User.objects.create(phone='+70000000009',full_name='ТЕСТ миграция',password='!',must_change_password=False)
            binding=Binding.objects.create(user_id=user.pk,max_user=4321,chat=5432,session_version=1)
            old=Watch.objects.create(binding_id=binding.pk,digest='existing-digest',version=7)
            executor=MigrationExecutor(connection);executor.migrate([('maxbot','0006_demandwatch_format_version')])
            from maxbot.models import DemandWatch
            current=DemandWatch.objects.get(pk=old.pk)
            self.assertEqual((current.digest,current.version,current.format_version),('existing-digest',7,1))
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
