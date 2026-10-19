import hashlib
import io
import json
import secrets
import shutil
import tempfile
import zipfile
from pathlib import Path
from django.core.management import call_command
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase,Client,override_settings
from django.urls import reverse
from accounts.models import User,AccessGrant,Reminder,Favorite,Preference
from projects.models import LegalEntity,ProjectObject,ObjectAccess,Contract,COMPANY_TAX_ID
from .models import Document,DocumentVersion,DocumentAccess,Bundle,BundleAccess,DocumentRead,Person,Comment,Notice
from .access import visible_documents,unread_documents,unsorted_documents
from .storage import validate_upload,private_path

class DocumentTests(TransactionTestCase):
    def setUp(self):
        self.root=tempfile.mkdtemp(prefix='armada-docs-v4-');self.override=override_settings(PRIVATE_DOCUMENT_ROOT=self.root)
        self.override.enable();self.addCleanup(self.override.disable);self.addCleanup(shutil.rmtree,self.root)
        password=secrets.token_urlsafe(24)
        self.leader=User.objects.create_user('+70000000001',password,full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        self.second=User.objects.create_user('+70000000002',password,full_name='ТЕСТ Второй',leader_slot=2,must_change_password=False)
        self.employee=User.objects.create_user('+70000000003',password,full_name='ТЕСТ Техник',role='foreman',must_change_password=False)
        self.other=User.objects.create_user('+70000000004',password,full_name='ТЕСТ Эколог',role='ecology',must_change_password=False)
        self.company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ Компания'})[0]
        self.obj=ProjectObject.objects.create(legal_entity=self.company,name='ТЕСТ Объект')
        ObjectAccess.objects.create(project_object=self.obj,user=self.employee,granted_by=self.leader)
        AccessGrant.objects.create(user=self.employee,permission='document.upload',granted_by=self.leader)
        self.client.force_login(self.leader)
    def file(self,text=b'%PDF-1.7\nsynthetic\n%%EOF',name='test.pdf'):return SimpleUploadedFile(name,text)
    def upload(self,user=None,**extra):
        self.client.force_login(user or self.leader)
        return self.client.post(reverse('document_upload'),{'title':'ТЕСТ документ','section':'general','file':self.file(),'collision':'both',**extra})
    def doc(self,user=None,**extra):
        response=self.upload(user,**extra);self.assertEqual(response.status_code,302,str(getattr(response,'content',''))[:300]);return Document.objects.latest('created_at')
    def download(self,doc,user,version=None):
        user.refresh_from_db();self.client.force_login(user);return self.client.get(reverse('document_download',args=[doc.pk,(version or doc.current_version).pk]))
    def grant(self,doc,user,replace=False):return DocumentAccess.objects.create(document=doc,user=user,enabled=True,can_replace=replace,granted_by=self.leader)
    def test_upload_optional_links_preserves_private_bytes_without_approval(self):
        doc=self.doc(self.employee);v=doc.current_version
        self.assertEqual(doc.objects_linked.count(),0);self.assertEqual(v.status,'available')
        self.assertEqual(v.sha256,hashlib.sha256(private_path(v.storage_key).read_bytes()).hexdigest())
        self.assertEqual(len(list(Path(self.root).glob('*/*'))),1)
    def test_owner_and_two_leaders_only_until_explicit_share(self):
        doc=self.doc(self.employee)
        self.assertEqual(self.download(doc,self.other).status_code,404)
        for u in [self.leader,self.second,self.employee]:self.assertEqual(self.download(doc,u).status_code,200)
    def test_object_assignment_never_opens_files(self):
        doc=self.doc(linked_objects=[str(self.obj.pk)])
        self.assertEqual(self.download(doc,self.employee).status_code,404)
    def test_share_does_not_require_object_or_parent_contract_visibility(self):
        contract=Contract.objects.create(legal_entity=self.company,number='SECRET-CONTRACT',subject='SECRET-SUBJECT')
        doc=self.doc(self.employee);doc.contracts_linked.add(contract);doc.objects_linked.add(self.obj);self.grant(doc,self.other)
        self.assertEqual(self.download(doc,self.other).status_code,200)
        self.client.force_login(self.other);r=self.client.get(reverse('document_detail',args=[doc.pk]))
        self.assertNotContains(r,contract.number);self.assertNotContains(r,contract.subject);self.assertNotContains(r,self.obj.name)
    def test_shared_reader_cannot_share_archive_or_replace(self):
        doc=self.doc(self.employee);self.grant(doc,self.other);self.client.force_login(self.other)
        for name in ['document_share','document_replace','document_links']:
            self.assertEqual(self.client.get(reverse(name,args=[doc.pk])).status_code,403)
        self.assertEqual(self.client.post(reverse('document_archive',args=[doc.pk]),{'expected_revision':doc.revision}).status_code,403)
    def test_author_can_share_and_revoke(self):
        doc=self.doc(self.employee);self.client.force_login(self.employee)
        data={'expected_revision':doc.revision,'employee':self.other.pk,'enabled':'on'}
        self.assertEqual(self.client.post(reverse('document_share',args=[doc.pk]),data).status_code,302)
        self.assertEqual(self.download(doc,self.other).status_code,200)
        doc.refresh_from_db();self.client.force_login(self.employee)
        self.assertEqual(self.client.post(reverse('document_share',args=[doc.pk]),{'expected_revision':doc.revision,'employee':self.other.pk}).status_code,302)
        self.assertEqual(self.download(doc,self.other).status_code,404)
    def test_bundle_access_inherits_future_files_and_denial_overrides(self):
        b=Bundle.objects.create(title='ТЕСТ комплект',created_by=self.employee,legal_entity=self.company)
        BundleAccess.objects.create(bundle=b,user=self.other,granted_by=self.employee)
        doc=self.doc(self.employee,bundle=b.pk)
        self.assertEqual(self.download(doc,self.other).status_code,200)
        DocumentAccess.objects.create(document=doc,user=self.other,enabled=False,granted_by=self.employee)
        self.assertEqual(self.download(doc,self.other).status_code,404)
    def test_new_version_preserves_links_access_and_immutable_old_bytes(self):
        doc=self.doc(self.employee,linked_objects=[str(self.obj.pk)]);self.grant(doc,self.other,True);old=doc.current_version
        content=private_path(old.storage_key).read_bytes();self.client.force_login(self.other)
        response=self.client.post(reverse('document_replace',args=[doc.pk]),{'expected_revision':doc.revision,'comment':'Учебная правка','file':self.file(b'changed','new.xls')})
        self.assertEqual(response.status_code,302);doc.refresh_from_db()
        self.assertEqual(doc.current_version.sequence,2);self.assertEqual(private_path(old.storage_key).read_bytes(),content)
        self.assertTrue(doc.objects_linked.filter(pk=self.obj.pk).exists());self.assertEqual(self.download(doc,self.other,old).status_code,200)
    def test_stale_replace_does_not_add_blob(self):
        doc=self.doc(self.employee);self.client.force_login(self.employee)
        self.assertContains(self.client.post(reverse('document_replace',args=[doc.pk]),{'expected_revision':0,'file':self.file()}),'уже изменён')
        self.assertEqual(doc.versions.count(),1);self.assertEqual(len(list(Path(self.root).glob('*/*'))),1)
    def test_unread_per_person_and_own_upload_excluded(self):
        doc=self.doc(self.employee)
        self.assertFalse(unread_documents(self.employee).exists());self.assertTrue(unread_documents(self.leader).filter(pk=doc.pk).exists())
        self.client.force_login(self.leader);self.client.get(reverse('document_detail',args=[doc.pk]))
        self.assertFalse(unread_documents(self.leader).exists());self.assertTrue(unread_documents(self.second).filter(pk=doc.pk).exists())
    def test_new_version_marks_previous_reader_unread(self):
        doc=self.doc(self.employee);DocumentRead.objects.create(user=self.leader,document=doc,version=doc.current_version)
        self.client.force_login(self.employee);self.client.post(reverse('document_replace',args=[doc.pk]),{'expected_revision':doc.revision,'file':self.file()})
        self.assertTrue(unread_documents(self.leader).filter(pk=doc.pk).exists())
    def test_collision_by_name_not_content_both_and_replace(self):
        doc=self.doc(self.employee);self.client.force_login(self.employee)
        r=self.upload(self.employee,collision='ask',file=self.file(b'other bytes'))
        self.assertContains(r,'названием');self.assertEqual(Document.objects.count(),1)
        r=self.upload(self.employee,collision='replace');self.assertEqual(r.status_code,302)
        doc.refresh_from_db();self.assertEqual(doc.current_version.sequence,2)
        second=self.doc(self.employee);self.assertEqual(second.current_version.original_name,'test (2).pdf')
    def test_all_extensions_stored_as_opaque_attachments(self):
        for name in ['report.xls','drawing.dwg','archive.zip','program.exe','page.html']:
            data=validate_upload(self.file(b'opaque bytes',name));self.assertEqual(data['content_type'],'application/octet-stream')
        with self.assertRaises(ValidationError):validate_upload(self.file(b'',name='empty.bin'))
    def test_unsorted_and_bulk_linking(self):
        self.assertEqual(self.upload(self.employee,section='contracts').status_code,200)
        self.assertFalse(Document.objects.exists())
        doc=self.doc(self.employee,section='contracts',defer_link='on')
        self.assertTrue(unsorted_documents(self.employee).filter(pk=doc.pk).exists())
        contract=Contract.objects.create(legal_entity=self.company,number='ТЕСТ-ДОГ',subject='Тест')
        doc.contracts_linked.add(contract);self.assertFalse(unsorted_documents(self.employee).filter(pk=doc.pk).exists())
    def test_archiving_hides_recipient_but_retains_owner_and_bytes(self):
        doc=self.doc(self.employee);self.grant(doc,self.other);self.client.force_login(self.employee)
        self.client.post(reverse('document_archive',args=[doc.pk]),{'expected_revision':doc.revision})
        self.assertEqual(self.download(doc,self.other).status_code,404);self.assertEqual(self.download(doc,self.employee).status_code,200)
        self.assertTrue(private_path(doc.current_version.storage_key).exists())
    def test_comments_recipients_cannot_implicitly_grant_access(self):
        doc=self.doc(self.employee);self.client.force_login(self.employee)
        data={'text':'ТЕСТ комментарий','recipients':[self.other.pk]}
        self.client.post(reverse('document_comment',args=[doc.pk]),data);self.assertFalse(Comment.objects.exists())
        self.grant(doc,self.other);self.client.post(reverse('document_comment',args=[doc.pk]),data)
        self.assertEqual(Comment.objects.count(),1);self.assertEqual(Notice.objects.get().user_id,self.other.pk)
    def test_preview_of_executable_forbidden(self):
        doc=self.doc(self.employee,file=self.file(b'MZ test','test.exe'))
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse('document_preview_file',args=[doc.pk,doc.current_version_id])).status_code,403)
        response=self.download(doc,self.employee);self.assertIn('attachment',response['Content-Disposition'])
    def test_integrity_and_private_paths(self):
        doc=self.doc();path=private_path(doc.current_version.storage_key);path.chmod(0o600);path.write_bytes(b'broken')
        self.assertEqual(self.download(doc,self.leader).status_code,503)
        with self.assertRaises(ValueError):private_path('../photo/test')
    def test_bulk_zip_checks_every_selected_file(self):
        own=self.doc(self.employee);other=self.doc();self.client.force_login(self.employee)
        self.assertEqual(self.client.post(reverse('document_bulk'),{'documents':[own.pk,other.pk],'action':'zip'}).status_code,403)
        r=self.client.post(reverse('document_bulk'),{'documents':[own.pk],'action':'zip'})
        with zipfile.ZipFile(io.BytesIO(b''.join(r.streaming_content))) as z:self.assertEqual(len(z.namelist()),1)
    def test_global_search_does_not_disclose_hidden_document(self):
        self.doc(self.employee,title='ВИДИМЫЙ');self.doc(title='СКРЫТЫЙ')
        self.client.force_login(self.employee);r=self.client.get(reverse('documents'),{'q':'СКРЫТЫЙ'})
        self.assertEqual(r.context['page'].paginator.count,0)
    def test_person_card_does_not_create_account(self):
        AccessGrant.objects.create(user=self.employee,permission='personnel.manage',granted_by=self.leader);self.client.force_login(self.employee)
        response=self.client.post(reverse('person_new'),{'full_name':'ТЕСТ Работник','position':'Сварщик'})
        self.assertEqual(response.status_code,302);self.assertEqual(User.objects.count(),4);self.assertIsNone(Person.objects.get().user_id)
    def test_person_archive_preserves_documents(self):
        p=Person.objects.create(full_name='ТЕСТ Работник',created_by=self.leader,legal_entity=self.company)
        doc=self.doc();doc.personnel_linked.add(p);self.client.force_login(self.leader)
        self.assertEqual(self.client.post(reverse('person_archive',args=[p.pk]),{'expected_revision':p.revision}).status_code,302)
        p.refresh_from_db();self.assertTrue(p.archived);self.assertTrue(p.documents.filter(pk=doc.pk).exists())
    def test_revoke_removes_favorite_and_private_reminder_link(self):
        doc=self.doc(self.employee);rule=self.grant(doc,self.other)
        Favorite.objects.create(user=self.other,kind='document',entity_id=doc.pk)
        Reminder.objects.create(user=self.other,document=doc,text='ТЕСТ напоминание',date='2026-10-20')
        rule.enabled=False;rule.save();self.client.force_login(self.other)
        self.assertNotContains(self.client.get(reverse('reminders')),'ТЕСТ напоминание')
        self.assertNotContains(self.client.get(reverse('dashboard')),doc.title)
    def test_source_version_fields_remain_immutable(self):
        v=self.doc().current_version;v.sha256='0'*64
        with self.assertRaises(ValueError):v.save()
    def test_csrf_upload(self):
        c=Client(enforce_csrf_checks=True);c.force_login(self.leader)
        self.assertEqual(c.post(reverse('document_upload'),{}).status_code,403)
    def test_backup_restore_files_metadata_and_rights(self):
        doc=self.doc(self.employee);self.grant(doc,self.other,True);v=doc.current_version
        from finance.models import FinancialScope,FinancialImport,OpeningSnapshot,CashReceipt,PaymentAllocation
        from finance.services import summary
        financial_source=FinancialImport.objects.create(sha256='b'*64,original_name='synthetic-backup.xlsx',uploaded_by=self.leader,source_document=doc)
        scope=FinancialScope.objects.create(legal_entity=self.company,title='ТЕСТ финансы для восстановления',created_by=self.leader)
        OpeningSnapshot.objects.create(scope=scope,source=financial_source,total_defects=10,repaired_defects=4,advance_received=500,accepted=800,paid_for_work=600,advance_offset=200,declared_advance_balance=300,declared_pending=0)
        source=private_path(v.storage_key).read_bytes();fixture=io.StringIO();call_command('dumpdata','accounts','projects','documents','finance',stdout=fixture)
        with tempfile.TemporaryDirectory(prefix='armada-restore-') as root:
            backup=Path(root);(backup/'metadata.json').write_text(fixture.getvalue());shutil.copytree(self.root,backup/'files')
            call_command('flush',interactive=False,verbosity=0);shutil.rmtree(self.root);Path(self.root).mkdir()
            self.assertFalse(Document.objects.exists());call_command('loaddata',str(backup/'metadata.json'),verbosity=0);shutil.copytree(backup/'files',self.root,dirs_exist_ok=True)
        restored=Document.objects.get(pk=doc.pk)
        self.assertEqual(restored.current_version.sha256,hashlib.sha256(source).hexdigest())
        self.assertEqual(b''.join(self.download(restored,self.other).streaming_content),source)
        restored_scope=FinancialScope.objects.get(pk=scope.pk);self.assertEqual(summary(restored_scope)['reported_advance_balance'],300);self.assertEqual(restored_scope.opening.source.source_document_id,doc.pk)
    def test_blank_title_uses_filename_and_retry_token_never_duplicates(self):
        from uuid import uuid4
        token=uuid4()
        for _ in range(2):
            r=self.upload(self.employee,title='',upload_token=token,file=self.file(name='Автоматическое имя.xls'))
            self.assertEqual(r.status_code,302)
        self.assertEqual(Document.objects.count(),1);doc=Document.objects.get();self.assertEqual(doc.title,'Автоматическое имя.xls');self.assertEqual(doc.versions.count(),1)
    def test_cyrillic_case_collision_is_consistent(self):
        self.doc(self.employee,file=self.file(name='ДОГОВОР.xls'))
        response=self.upload(self.employee,file=self.file(name='договор.xls'),collision='ask')
        self.assertContains(response,'названием');self.assertEqual(Document.objects.count(),1)
    def test_link_change_requires_acknowledgement_of_inherited_access(self):
        first=Bundle.objects.create(title='ТЕСТ Первый',created_by=self.employee,legal_entity=self.company)
        second=Bundle.objects.create(title='ТЕСТ Второй',created_by=self.employee,legal_entity=self.company)
        BundleAccess.objects.create(bundle=first,user=self.other,granted_by=self.employee)
        doc=self.doc(self.employee,bundle=first.pk);self.client.force_login(self.employee)
        data={'title':doc.title,'section':doc.section,'bundle':second.pk,'expected_revision':doc.revision}
        r=self.client.post(reverse('document_links',args=[doc.pk]),data)
        self.assertContains(r,'Потеряют доступ');doc.refresh_from_db();self.assertEqual(doc.bundle_id,first.pk)
        self.assertEqual(self.client.post(reverse('document_links',args=[doc.pk]),{**data,'confirm_access':'on'}).status_code,302)
        self.assertEqual(self.download(doc,self.other).status_code,404)
    def test_responsibility_transfer_preserves_uploader(self):
        doc=self.doc(self.employee);self.client.force_login(self.leader)
        r=self.client.post(reverse('transfer_documents',args=[self.employee.pk]),{'recipient':self.other.pk,'documents':[doc.pk]})
        self.assertEqual(r.status_code,302);doc.refresh_from_db();self.assertEqual(doc.created_by_id,self.employee.pk);self.assertEqual(doc.responsible_id,self.other.pk)
        self.assertEqual(self.download(doc,self.other).status_code,200)
    def test_personnel_export_only_selected_available_documents(self):
        from .models import PersonAssignment
        p=Person.objects.create(full_name='ТЕСТ Работник',created_by=self.leader,legal_entity=self.company)
        PersonAssignment.objects.create(person=p,project_object=self.obj,created_by=self.leader)
        doc=self.doc();doc.personnel_linked.add(p);url=reverse('personnel_export',args=[self.obj.pk])
        self.client.force_login(self.leader)
        self.assertContains(self.client.post(url,{'people':[p.pk]}),doc.title)
        response=self.client.post(url,{'people':[p.pk],'documents':[doc.pk],'download':'1'})
        with zipfile.ZipFile(io.BytesIO(b''.join(response.streaming_content))) as z:
            self.assertEqual(len(z.namelist()),1);self.assertIn('ТЕСТ Работник',z.namelist()[0])
    def test_bulk_share_then_bulk_link_uses_one_checked_form(self):
        first=self.doc(self.employee,title='ТЕСТ пачка 1');second=self.doc(self.employee,title='ТЕСТ пачка 2')
        self.client.force_login(self.employee);url=reverse('document_bulk');ids=[first.pk,second.pk]
        response=self.client.post(url,{'documents':ids,'action':'share'})
        self.assertFalse(response.context['form'].is_bound)
        snapshot=response.context['version_snapshot']
        response=self.client.post(url,{'documents':ids,'action':'share','apply':'1','version_snapshot':snapshot,'employee':self.other.pk,'enabled':'on','can_replace':'on'})
        self.assertEqual(response.status_code,302);self.assertEqual(DocumentAccess.objects.filter(user=self.other,enabled=True,can_replace=True).count(),2)
        response=self.client.post(url,{'documents':ids,'action':'links'});snapshot=response.context['version_snapshot']
        response=self.client.post(url,{'documents':ids,'action':'links','apply':'1','version_snapshot':snapshot,'linked_objects':[self.obj.pk]})
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.obj.documents.filter(pk__in=ids).count(),2)
    def test_stale_bulk_operation_changes_nothing(self):
        doc=self.doc(self.employee);self.client.force_login(self.employee);url=reverse('document_bulk')
        response=self.client.post(url,{'documents':[doc.pk],'action':'share'});snapshot=response.context['version_snapshot']
        doc.revision+=1;doc.save(update_fields=['revision'])
        response=self.client.post(url,{'documents':[doc.pk],'action':'share','apply':'1','version_snapshot':snapshot,'employee':self.other.pk,'enabled':'on'})
        self.assertEqual(response.status_code,409);self.assertFalse(DocumentAccess.objects.filter(document=doc,user=self.other).exists())
    def test_external_role_does_not_receive_other_contractors_directory(self):
        external=User.objects.create_user('+70000000005',secrets.token_urlsafe(24),full_name='ТЕСТ внешний участник',role='subcontractor',must_change_password=False)
        competitor=User.objects.create_user('+70000000006',secrets.token_urlsafe(24),full_name='СКРЫТЫЙ ДРУГОЙ ПОДРЯДЧИК',role='subcontractor',must_change_password=False)
        AccessGrant.objects.create(user=external,permission='document.upload',granted_by=self.leader)
        self.client.force_login(external)
        self.assertNotContains(self.client.get(reverse('document_upload')),competitor.full_name)
        doc=self.doc(external)
        self.assertEqual(self.client.get(reverse('document_share',args=[doc.pk])).status_code,403)

    def test_simple_share_levels_revoke_without_broadening_permissions(self):
        doc=self.doc(self.employee)
        for level,enabled,replace in [('view',True,False),('versions',True,True),('closed',False,False)]:
            self.client.force_login(self.employee);doc.refresh_from_db()
            response=self.client.post(reverse('document_share',args=[doc.pk]),{'expected_revision':doc.revision,'employee':self.other.pk,'permission':level})
            self.assertEqual(response.status_code,302)
            rule=doc.access_rules.get(user=self.other);self.assertEqual((rule.enabled,rule.can_replace),(enabled,replace))
            self.assertEqual(visible_documents(self.other).filter(pk=doc.pk).exists(),enabled)

    def test_new_bundle_can_have_primary_without_preexisting_bundle(self):
        response=self.upload(self.employee,bundle_title='ТЕСТ новый комплект',is_primary='on')
        self.assertEqual(response.status_code,302)
        doc=Document.objects.latest('created_at');self.assertTrue(doc.is_primary);self.assertEqual(doc.bundle.title,'ТЕСТ новый комплект')

    def test_single_file_grant_does_not_reveal_bundle_title_in_grouped_list(self):
        b=Bundle.objects.create(title='СКРЫТОЕ ИМЯ КОМПЛЕКТА',created_by=self.leader,legal_entity=self.company)
        doc=self.doc(self.leader,bundle=b.pk);self.grant(doc,self.other);self.client.force_login(self.other)
        response=self.client.get(reverse('documents')+'?mode=all')
        self.assertContains(response,doc.title);self.assertNotContains(response,b.title)

    def test_upload_context_uses_allowed_parent_and_defaults_deferred_organization(self):
        person=Person.objects.create(legal_entity=self.company,created_by=self.leader,full_name='ТЕСТ работник')
        self.client.force_login(self.leader)
        response=self.client.get(reverse('document_upload')+'?person='+str(person.pk))
        self.assertEqual(response.context['form'].initial['linked_people'],[person.pk]);self.assertEqual(response.context['form'].initial['section'],'personnel')
        self.assertTrue(response.context['form'].initial['defer_link']);self.assertEqual(response.context['destination'],person.full_name)
        AccessGrant.objects.create(user=self.other,permission='document.upload',granted_by=self.leader)
        self.client.force_login(self.other)
        response=self.client.get(reverse('document_upload')+'?person='+str(person.pk))
        self.assertIsNone(response.context['destination']);self.assertNotContains(response,person.full_name)
