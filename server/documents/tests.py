import hashlib
import io
import json
import secrets
import shutil
import tempfile
from pathlib import Path
from django.core.management import call_command
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase, Client, override_settings
from django.urls import reverse
from accounts.models import User, AccessGrant, AuditEvent
from projects.models import LegalEntity, ProjectObject, ObjectAccess, Contract, ContractAccess, ContractObject, COMPANY_TAX_ID
from .models import Document, DocumentVersion, DocumentAccess, ApprovalEvent
from .access import visible_documents
from .storage import validate_upload, private_path

class DocumentTests(TransactionTestCase):
    def setUp(self):
        self.root=tempfile.mkdtemp(prefix='armada-documents-')
        self.override=override_settings(PRIVATE_DOCUMENT_ROOT=self.root)
        self.override.enable();self.addCleanup(self.override.disable);self.addCleanup(shutil.rmtree,self.root)
        password=secrets.token_urlsafe(24)
        self.leader=User.objects.create_user('+70000000001',password,full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        self.second=User.objects.create_user('+70000000002',password,full_name='ТЕСТ Второй',leader_slot=2,must_change_password=False)
        self.employee=User.objects.create_user('+70000000003',password,full_name='ТЕСТ Мастер',role='foreman',must_change_password=False)
        self.other=User.objects.create_user('+70000000004',password,full_name='ТЕСТ Другой',role='foreman',must_change_password=False)
        self.company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ООО Армада-Сибирь'})[0]
        self.obj=ProjectObject.objects.create(legal_entity=self.company,name='ТЕСТ Объект')
        self.secret=ProjectObject.objects.create(legal_entity=self.company,name='ЧУЖОЙ ОБЪЕКТ')
        ObjectAccess.objects.create(project_object=self.obj,user=self.employee,granted_by=self.leader)
        for permission in ['read','upload','replace','download','archive','restore','export']:
            AccessGrant.objects.create(user=self.employee,permission='document.'+permission,granted_by=self.leader)
        self.client.force_login(self.leader)
    def file(self,text=b'%PDF-1.7\nsynthetic test only\n%%EOF',name='test.pdf'):
        return SimpleUploadedFile(name,text,content_type='application/pdf')
    def upload(self,user=None,**extra):
        self.client.force_login(user or self.leader)
        data={'title':'ТЕСТ документ','section':'foreman','kind':'work','linked_objects':[str(self.obj.pk)],'file':self.file(),**extra}
        return self.client.post(reverse('document_upload'),data)
    def doc(self,user=None,**extra):
        response=self.upload(user,**extra);self.assertEqual(response.status_code,302)
        return Document.objects.latest('created_at')
    def download(self,doc,user,version=None):
        self.client.force_login(user)
        return self.client.get(reverse('document_download',args=[doc.pk,(version or doc.current_version).pk]))
    def share(self,doc,user,**flags):
        return DocumentAccess.objects.create(document=doc,user=user,enabled=True,granted_by=self.leader,**flags)
    def test_upload_stores_private_bytes_hash_and_links_once(self):
        doc=self.doc();v=doc.current_version
        self.assertEqual(doc.versions.count(),1);self.assertEqual(doc.objects_linked.count(),1)
        data=private_path(v.storage_key).read_bytes()
        self.assertEqual(v.sha256,hashlib.sha256(data).hexdigest());self.assertEqual(v.size,len(data))
        self.assertEqual(v.status,'pending');self.assertEqual(len(list(Path(self.root).glob('*/*'))),1)
    def test_employee_owns_documents_but_not_other_uploads(self):
        own=self.doc(self.employee);foreign=self.doc()
        self.assertEqual(list(visible_documents(self.employee)),[own])
        self.assertEqual(self.download(foreign,self.employee).status_code,404)
        self.assertEqual(self.download(own,self.employee).status_code,200)
    def test_employee_cannot_upload_outside_assignment_or_role(self):
        for changes in [{'linked_objects':[str(self.secret.pk)]},{'section':'accounting'},{'kind':'personal'},{'linked_objects':[]}]:
            self.assertEqual(self.upload(self.employee,**changes).status_code,200)
        self.assertFalse(Document.objects.exists());self.assertEqual(list(Path(self.root).glob('*/*')),[])
    def test_explicit_deny_overrides_owner(self):
        doc=self.doc(self.employee)
        DocumentAccess.objects.create(document=doc,user=self.employee,enabled=False,granted_by=self.leader)
        self.assertEqual(self.download(doc,self.employee).status_code,404)
    def test_explicit_share_needs_download_flag_and_global_permission(self):
        doc=self.doc();rule=self.share(doc,self.employee)
        self.assertEqual(self.download(doc,self.employee).status_code,403)
        rule.can_download=True;rule.save()
        self.assertEqual(self.download(doc,self.employee).status_code,200)
        self.employee.grants.filter(permission='document.download').delete()
        self.assertEqual(self.download(doc,self.employee).status_code,403)
    def test_revoked_object_or_global_read_immediately_denies(self):
        doc=self.doc(self.employee);ObjectAccess.objects.filter(user=self.employee).delete()
        self.assertEqual(self.download(doc,self.employee).status_code,404)
    def test_all_linked_objects_must_be_visible(self):
        doc=self.doc(self.employee);doc.objects_linked.add(self.secret)
        self.assertEqual(self.download(doc,self.employee).status_code,404)
    def test_contract_visibility_required_and_number_not_leaked(self):
        contract=Contract.objects.create(legal_entity=self.company,number='SECRET-CONTRACT-NUMBER',subject='SECRET-SUBJECT')
        ContractObject.objects.create(contract=contract,project_object=self.obj)
        doc=self.doc(self.employee);doc.contracts_linked.add(contract)
        self.assertEqual(self.download(doc,self.employee).status_code,404)
        ContractAccess.objects.create(contract=contract,user=self.employee,granted_by=self.leader)
        self.client.force_login(self.employee)
        response=self.client.get(reverse('document_detail',args=[doc.pk]))
        self.assertNotContains(response,contract.number);self.assertNotContains(response,contract.subject)
    def test_new_version_current_pending_and_old_bytes_preserved(self):
        doc=self.doc(self.employee);old=doc.current_version;oldbytes=private_path(old.storage_key).read_bytes()
        response=self.client.post(reverse('document_replace',args=[doc.pk]),{'expected_revision':1,'comment':'Исправлено','file':self.file(b'%PDF-1.7\nnew\n%%EOF')})
        self.assertEqual(response.status_code,302);doc.refresh_from_db()
        self.assertEqual(doc.current_version.sequence,2);self.assertEqual(doc.current_version.status,'pending')
        self.assertEqual(private_path(old.storage_key).read_bytes(),oldbytes)
    def test_stale_replace_does_not_store_blob(self):
        doc=self.doc(self.employee)
        response=self.client.post(reverse('document_replace',args=[doc.pk]),{'expected_revision':0,'comment':'test','file':self.file()})
        self.assertContains(response,'уже изменён');self.assertEqual(doc.versions.count(),1)
        self.assertEqual(len(list(Path(self.root).glob('*/*'))),1)
    def test_versions_are_immutable(self):
        doc=self.doc();v=doc.current_version;v.sha256='0'*64
        with self.assertRaises(ValueError):v.save()
    def decide(self,doc,user=None,decision='revision',reason='Исправить',version=None):
        self.client.force_login(user or self.leader)
        return self.client.post(reverse('document_approve',args=[doc.pk,(version or doc.current_version).pk]),{'decision':decision,'reason':reason})
    def test_rejection_disables_employee_download_preserves_review(self):
        doc=self.doc(self.employee);self.assertEqual(self.decide(doc).status_code,302)
        self.assertEqual(self.download(doc,self.employee).status_code,403)
        self.assertEqual(self.download(doc,self.second).status_code,200)
        self.assertEqual(ApprovalEvent.objects.get().reason,'Исправить')
    def test_single_leader_decision_and_reason_required(self):
        doc=self.doc();self.assertEqual(self.decide(doc,reason='').status_code,200)
        self.assertFalse(ApprovalEvent.objects.exists())
        self.assertEqual(self.decide(doc,decision='approved',reason='').status_code,302)
        self.assertEqual(self.decide(doc,self.second).status_code,409)
        self.assertEqual(ApprovalEvent.objects.count(),1)
    def test_employee_cannot_approve_or_share(self):
        doc=self.doc(self.employee)
        self.assertEqual(self.decide(doc,self.employee).status_code,403)
        self.assertEqual(self.client.get(reverse('document_share',args=[doc.pk])).status_code,403)
    def test_archive_prevents_employee_download_and_restore_retains_file(self):
        doc=self.doc(self.employee);key=doc.current_version.storage_key
        self.assertEqual(self.client.post(reverse('document_archive',args=[doc.pk]),{'expected_revision':1}).status_code,302)
        self.assertEqual(self.download(doc,self.employee).status_code,403)
        self.assertEqual(self.client.post(reverse('document_archive',args=[doc.pk]),{'expected_revision':2}).status_code,302)
        self.assertEqual(self.download(doc,self.employee).status_code,200);self.assertTrue(private_path(key).exists())
    def test_file_integrity_failure_returns_503(self):
        doc=self.doc();p=private_path(doc.current_version.storage_key);p.chmod(0o600);p.write_bytes(b'broken')
        self.assertEqual(self.download(doc,self.leader).status_code,503)
        self.assertTrue(AuditEvent.objects.filter(action='document_integrity_error').exists())
    def test_download_is_attachment_private_and_nosniff(self):
        doc=self.doc();response=self.download(doc,self.leader)
        self.assertIn('attachment',response['Content-Disposition']);self.assertEqual(response['X-Content-Type-Options'],'nosniff')
        self.assertIn('no-store',response['Cache-Control']);self.assertEqual(b''.join(response.streaming_content),self.file().read())
    def test_anonymous_file_and_storage_path_not_public(self):
        doc=self.doc();self.client.logout()
        self.assertEqual(self.client.get(reverse('document_download',args=[doc.pk,doc.current_version_id])).status_code,302)
        self.assertEqual(self.client.get('/media/'+doc.current_version.storage_key).status_code,404)
    def test_history_requires_separate_share(self):
        doc=self.doc();old=doc.current_version
        self.client.post(reverse('document_replace',args=[doc.pk]),{'expected_revision':1,'comment':'new','file':self.file()})
        rule=self.share(doc,self.employee,can_download=True)
        self.assertEqual(self.download(doc,self.employee,old).status_code,403)
        rule.can_history=True;rule.save();self.assertEqual(self.download(doc,self.employee,old).status_code,200)
    def test_search_export_scoped_and_formula_safe(self):
        own=self.doc(self.employee,title='=TESTFORMULA');self.doc(title='SECRET-TITLE')
        self.client.force_login(self.employee)
        self.assertNotContains(self.client.get(reverse('documents')), 'SECRET-TITLE')
        response=self.client.get(reverse('document_export'));self.assertContains(response,"'=TESTFORMULA");self.assertNotContains(response,'SECRET-TITLE')
    def test_invalid_files_and_traversal_rejected(self):
        for name,data in [('bad.pdf',b'not pdf'),('bad.exe',b'%PDF-'),('bad.docx',b'invalid'),('bad.txt',b'\x00')]:
            with self.assertRaises(ValidationError):validate_upload(self.file(data,name))
        with self.assertRaises(ValueError):private_path('../photo/test')
    def test_csrf_protects_upload(self):
        client=Client(enforce_csrf_checks=True);client.force_login(self.leader)
        self.assertEqual(client.post(reverse('document_upload'),{}).status_code,403)
    def test_backup_restore_metadata_and_files(self):
        doc=self.doc(self.employee);self.decide(doc,decision='approved',reason='Проверено')
        doc.objects_linked.add(self.secret);self.share(doc,self.employee,can_history=True)
        v=doc.current_version;content=private_path(v.storage_key).read_bytes()
        fixture=io.StringIO();call_command('dumpdata','accounts','projects','documents',stdout=fixture)
        data=json.loads(fixture.getvalue());self.assertTrue(any(row['model']=='documents.approvalevent' for row in data))
        with tempfile.TemporaryDirectory(prefix='armada-restore-') as backup_root:
            backup=Path(backup_root)
            (backup/'metadata.json').write_text(fixture.getvalue())
            shutil.copytree(self.root,backup/'files')
            call_command('flush',interactive=False,verbosity=0)
            shutil.rmtree(self.root);Path(self.root).mkdir()
            self.assertFalse(Document.objects.exists())
            self.assertFalse(private_path(v.storage_key).exists())
            call_command('loaddata',str(backup/'metadata.json'),verbosity=0)
            shutil.copytree(backup/'files',self.root,dirs_exist_ok=True)
        restored=Document.objects.get(pk=doc.pk)
        self.assertEqual(restored.objects_linked.count(),2);self.assertEqual(restored.current_version.sha256,hashlib.sha256(content).hexdigest())
        self.assertEqual(restored.current_version.status,'approved');self.assertEqual(restored.current_version.decision.reason,'Проверено')
        self.assertEqual(b''.join(self.download(restored,self.leader).streaming_content),content)
    def test_leader_share_changes_session_and_blocks_stale_update(self):
        doc=self.doc();old_epoch=self.employee.session_version
        data={'expected_revision':1,'employee':str(self.employee.pk),'enabled':'on','can_download':'on','can_history':'on'}
        self.assertEqual(self.client.post(reverse('document_share',args=[doc.pk]),data).status_code,302)
        rule=DocumentAccess.objects.get(document=doc,user=self.employee)
        self.assertTrue(rule.enabled and rule.can_download and rule.can_history)
        self.employee.refresh_from_db();self.assertEqual(self.employee.session_version,old_epoch+1)
        response=self.client.post(reverse('document_share',args=[doc.pk]),{**data,'enabled':''})
        self.assertContains(response,'уже изменён');rule.refresh_from_db();self.assertTrue(rule.enabled)
    def test_personal_files_need_category_permission_and_explicit_share(self):
        doc=self.doc(kind='personal',linked_people=[str(self.employee.pk)])
        self.share(doc,self.employee,can_download=True)
        self.assertEqual(self.download(doc,self.employee).status_code,404)
        AccessGrant.objects.create(user=self.employee,permission='document.personal',granted_by=self.leader)
        self.assertEqual(self.download(doc,self.employee).status_code,200)
    def test_decision_on_old_version_does_not_approve_current(self):
        doc=self.doc();old=doc.current_version
        self.client.post(reverse('document_replace',args=[doc.pk]),{'expected_revision':1,'comment':'new','file':self.file()})
        self.assertEqual(self.decide(doc,decision='approved',reason='',version=old).status_code,302)
        doc.refresh_from_db();self.assertEqual(doc.current_version.status,'pending')
        self.assertEqual(doc.versions.get(pk=old.pk).status,'approved')
