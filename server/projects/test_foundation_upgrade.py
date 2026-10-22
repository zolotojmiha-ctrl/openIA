"""Migrate the previous release without inferring a contract from an object."""
import secrets
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

class FoundationUpgradeTests(TransactionTestCase):
    def test_old_work_files_permissions_and_finance_survive_with_unassigned_contract(self):
        executor=MigrationExecutor(connection);latest=executor.loader.graph.leaf_nodes()
        before=[('projects','0002_company'),('repairs','0007_alter_coupling_wall_mm_alter_job_method_and_more'),('accounts','0012_alter_auditevent_action')]
        try:
            executor.migrate(before);old=executor.loader.project_state(before).apps
            User=old.get_model('accounts','User');company=old.get_model('projects','LegalEntity').objects.get_or_create(tax_id='8604073687',defaults={'name':'ТЕСТ Компания'})[0]
            leader=User.objects.create(phone='+70000000001',full_name='ТЕСТ руководитель',leader_slot=1,password='hash-placeholder',must_change_password=False,session_version=7)
            worker=User.objects.create(phone='+70000000002',full_name='ТЕСТ мастер',password='hash-placeholder',session_version=5)
            obj=old.get_model('projects','ProjectObject').objects.create(legal_entity=company,name='Одна площадка')
            contracts=[]
            for number in ['А','Б']:
                c=old.get_model('projects','Contract').objects.create(legal_entity=company,number=number,subject='Два возможных контракта');contracts.append(c)
                old.get_model('projects','ContractObject').objects.create(contract=c,project_object=obj)
            old.get_model('projects','ObjectAccess').objects.create(user=worker,project_object=obj,granted_by=leader)
            old.get_model('projects','ContractAccess').objects.create(user=worker,contract=contracts[0],granted_by=leader,show_number=False,show_subject=True)
            old.get_model('accounts','AccessGrant').objects.create(user=worker,permission='repairs.prepare',granted_by=leader)
            section=old.get_model('repairs','Section').objects.create(project_object=obj,rnu='Омское',pipeline='Омск–Татарск',name='Участок')
            job=old.get_model('repairs','Job').objects.create(section=section,method='p2',length_m=1,state='confirmed',assigned_to=worker,created_by=leader,version=9,accepted_date=timezone.localdate())
            defect=old.get_model('repairs','Defect').objects.create(section=section,kilometre='153.546500',number='0001',pipe_section='001')
            old.get_model('repairs','JobDefect').objects.create(job=job,defect=defect)
            report=old.get_model('repairs','Report').objects.create(job=job,author=worker,kind='complete',performed_date=timezone.localdate(),latitude=55,longitude=73,confirmed_by=leader,outcome='complete')
            attachment=old.get_model('repairs','Attachment').objects.create(report=report,storage_key='test-no-real-file',name='original.jpg',content_type='image/jpeg',size=123,sha256='a'*64)
            executor=MigrationExecutor(connection);executor.migrate(latest);new=executor.loader.project_state(latest).apps
            restored=new.get_model('repairs','Job').objects.get(pk=job.pk)
            self.assertIsNone(restored.contract_id);self.assertEqual(restored.version,9);self.assertEqual(restored.state,'confirmed');self.assertEqual(restored.accepted_date,job.accepted_date)
            d=new.get_model('repairs','Defect').objects.get(pk=defect.pk);self.assertIsNone(d.contract_id);self.assertEqual(str(d.kilometre),'153.546500');self.assertEqual(d.number,'0001')
            self.assertEqual(new.get_model('repairs','Attachment').objects.get(pk=attachment.pk).sha256,'a'*64)
            self.assertEqual(new.get_model('repairs','Report').objects.get(pk=report.pk).author_id,worker.pk)
            rule=new.get_model('projects','ContractAccess').objects.get(user_id=worker.pk);self.assertFalse(rule.show_number);self.assertTrue(rule.show_subject)
            self.assertEqual(new.get_model('accounts','User').objects.get(pk=worker.pk).session_version,5)
            self.assertEqual(new.get_model('accounts','AccessGrant').objects.get(user_id=worker.pk).permission,'repairs.prepare')
            self.assertEqual(new.get_model('projects','Contract').objects.get(pk=contracts[0].pk).work_kinds,[])
        finally:MigrationExecutor(connection).migrate(latest)
