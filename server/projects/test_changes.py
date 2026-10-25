import uuid,io
from datetime import date
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import PermissionDenied,ValidationError
from accounts.models import User,AccessGrant
from documents.models import Document,DocumentVersion
from .models import LegalEntity,ProjectObject,Contract,ContractObject,ObjectAccess,ContractAccess,COMPANY_TAX_ID,ContractChange,ContractChangeRevision,ContractChangeCommercial
from .changes import ChangeForm,submit,decide

class CustomerChangeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.leader=User.objects.create_user('+70000000601','test-secret',full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        cls.tech=User.objects.create_user('+70000000602','test-secret',full_name='ТЕСТ Техник',role='technician',must_change_password=False)
        cls.worker=User.objects.create_user('+70000000603','test-secret',full_name='ТЕСТ Мастер',role='foreman',must_change_password=False)
        cls.entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
        cls.obj=ProjectObject.objects.create(legal_entity=cls.entity,name='ТЕСТ 214 км')
        cls.contract=Contract.objects.create(legal_entity=cls.entity,number='Скрытый номер',subject='Подготовка к диагностике',work_kinds=['diagnostics'],amount='100000.00')
        ContractObject.objects.create(contract=cls.contract,project_object=cls.obj)
        for user in [cls.tech,cls.worker]:
            ObjectAccess.objects.create(user=user,project_object=cls.obj,granted_by=cls.leader)
            ContractAccess.objects.create(user=user,contract=cls.contract,granted_by=cls.leader)
        AccessGrant.objects.create(user=cls.tech,permission='contract.changes',granted_by=cls.leader)
    def data(self,change=None,user=None,**updates):
        user=user or self.tech
        values={'token':str(uuid.uuid4()),'expected_version':change.version if change else 0,'title':'Сократить ширину зачистки','received_on':'2026-07-15','effective_from':'2026-07-16','objects':[str(self.obj.pk)],'scope':'Шлифовка секций 5–12','before_text':'300 мм','after_text':'200 мм','basis_kind':'oral','basis_note':'ТЕСТ указание заказчика','impacts':['volumes','cost'],**updates}
        form=ChangeForm(values,user=user,contract=self.contract,change=change);self.assertTrue(form.is_valid(),form.errors.as_json());return form.cleaned_data
    def create(self):return submit(self.tech,self.contract.pk,None,self.data())
    def approve(self,change):return decide(self.leader,change.pk,change.version,'approved','Подтверждено для последующих работ')
    def test_role_alone_cannot_create_or_read_register(self):
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse('contract_changes',args=[self.contract.pk])).status_code,403)
        self.assertEqual(self.client.get(reverse('contract_change_new',args=[self.contract.pk])).status_code,403)
    def test_new_submission_retry_creates_one_revision(self):
        data=self.data();a=submit(self.tech,self.contract.pk,None,data);b=submit(self.tech,self.contract.pk,None,data)
        self.assertEqual(a.pk,b.pk);self.assertEqual(ContractChange.objects.count(),1);self.assertEqual(a.revisions.count(),1)
        self.assertEqual(a.state,'review');self.assertIsNone(a.active_revision)
    def test_changed_payload_cannot_reuse_token(self):
        data=self.data();submit(self.tech,self.contract.pk,None,data);data['after_text']='100 мм'
        with self.assertRaises(ValidationError):submit(self.tech,self.contract.pk,None,data)
        self.assertEqual(ContractChangeRevision.objects.count(),1)
    def test_leader_review_is_separate_from_financial_approval(self):
        change=self.approve(self.create());self.assertEqual(change.active_revision.after_text,'200 мм')
        self.assertFalse(ContractChangeCommercial.objects.exists());self.contract.refresh_from_db();self.assertEqual(str(self.contract.amount),'100000.00')
        with self.assertRaises(ValidationError):decide(self.leader,change.pk,change.version,'signed','Нужно оформить')
    def test_return_and_resubmit_keeps_earlier_revision(self):
        change=self.create();old=change.current_revision
        change=decide(self.leader,change.pk,change.version,'revision','Уточните границы')
        change=submit(self.tech,self.contract.pk,change.pk,self.data(change,scope='Секции 7–12'))
        old.refresh_from_db();self.assertEqual(old.scope,'Шлифовка секций 5–12');self.assertEqual(change.revisions.count(),2)
        self.assertEqual(old.decision.note,'Уточните границы')
    def test_approved_history_survives_new_pending_revision(self):
        change=self.approve(self.create());approved=change.active_revision
        change=submit(self.tech,self.contract.pk,change.pk,self.data(change,after_text='250 мм'))
        self.assertEqual(change.active_revision,approved);self.assertEqual(change.state,'review');self.assertEqual(change.current_revision.after_text,'250 мм')
        approved.refresh_from_db();self.assertEqual(approved.after_text,'200 мм')
        decide(self.leader,change.pk,change.version,'pending','Нужно допсоглашение',revision=approved)
        self.assertEqual(approved.commercial_events.count(),1)
    def test_ordinary_author_cannot_review_and_stale_version_rejected(self):
        change=self.create()
        with self.assertRaises(PermissionDenied):decide(self.tech,change.pk,change.version,'approved','Да')
        self.approve(change)
        with self.assertRaises(ValidationError):decide(self.leader,change.pk,change.version,'revision','Поздний запрос')
    def test_pending_revision_cannot_be_overwritten(self):
        change=self.create()
        with self.assertRaises(ValidationError):submit(self.tech,self.contract.pk,change.pk,self.data(change))
    def test_acl_revocation_blocks_retry_and_decision(self):
        data=self.data();submit(self.tech,self.contract.pk,None,data)
        AccessGrant.objects.filter(user=self.tech).delete()
        with self.assertRaises(PermissionDenied):submit(self.tech,self.contract.pk,None,data)
    def test_no_hidden_contract_fields_in_register(self):
        change=self.create();self.client.force_login(self.tech)
        response=self.client.get(reverse('contract_change',args=[change.pk]));self.assertContains(response,'200 мм');self.assertNotContains(response,'Скрытый номер');self.assertNotContains(response,'100000.00')
    def test_partial_object_scope_denies_register(self):
        other=ProjectObject.objects.create(legal_entity=self.entity,name='Закрытая площадка');ContractObject.objects.create(contract=self.contract,project_object=other)
        self.client.force_login(self.tech);self.assertEqual(self.client.get(reverse('contract_changes',args=[self.contract.pk])).status_code,403)
    def test_document_version_and_acl_are_preserved(self):
        doc=Document.objects.create(legal_entity=self.entity,title='Закрытое письмо',section='contracts',created_by=self.leader)
        version=DocumentVersion.objects.create(document=doc,sequence=1,storage_key='a'*40,original_name='test.pdf',content_type='application/pdf',size=1,sha256='0'*64,uploaded_by=self.leader)
        doc.current_version=version;doc.save()
        data=self.data(user=self.leader,basis_kind='document',basis_document=str(doc.pk))
        change=submit(self.leader,self.contract.pk,None,data)
        self.assertEqual(change.current_revision.basis_version,version)
        self.client.force_login(self.tech);response=self.client.get(reverse('contract_change',args=[change.pk]))
        self.assertNotContains(response,'Закрытое письмо');self.assertNotContains(response,str(doc.pk));self.assertContains(response,'доступ к файлу не выдан')
    def test_archive_blocks_all_content_changes(self):
        from .archive import lifecycle_write
        change=self.create()
        with lifecycle_write():Contract.objects.filter(pk=self.contract.pk).update(status='archived')
        with self.assertRaises(PermissionDenied):decide(self.leader,change.pk,change.version,'approved','Да')
        with self.assertRaises(PermissionDenied):ContractChangeRevision.objects.filter(change=change).update(after_text='Подмена')
    def test_new_notice_appears_for_leader(self):
        change=self.create();self.client.force_login(self.leader)
        self.assertContains(self.client.get(reverse('dashboard')),'Указания заказчика на рассмотрении')
        self.assertContains(self.client.get(reverse('contract_changes_review')),'Сократить ширину зачистки')
    def test_excel_preview_and_retry_do_not_approve_requests(self):
        from .changes_excel import HEADERS
        from repairs.excel import workbook
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.force_login(self.tech)
        row=['','Уменьшить зачистку','2026-07-15','2026-07-16',str(self.obj.pk),'Секции 5–12','300 мм','200 мм','Устное указание','ТЕСТ заказчик','','Объёмы;Стоимость','']
        response=self.client.post(reverse('contract_changes_import',args=[self.contract.pk]),{'file':SimpleUploadedFile('changes.xlsx',workbook([row],headers=HEADERS))})
        self.assertEqual(response.status_code,200);self.assertFalse(ContractChange.objects.exists());token=response.context['preview'];self.assertTrue(token)
        for _ in range(2):self.assertEqual(self.client.post(reverse('contract_changes_import',args=[self.contract.pk]),{'apply':'1','preview':token}).status_code,302)
        self.assertEqual(ContractChange.objects.count(),1);self.assertEqual(ContractChangeRevision.objects.count(),1);self.assertEqual(ContractChange.objects.get().state,'review')

    def test_commercial_notes_remain_leader_only(self):
        change=self.approve(self.create());decide(self.leader,change.pk,change.version,'pending','Закрытая финансовая оценка 456789')
        self.client.force_login(self.tech);response=self.client.get(reverse('contract_change',args=[change.pk]))
        self.assertNotContains(response,'456789');self.assertContains(response,'Требуется оформление')

    def test_import_error_rolls_back_earlier_rows(self):
        from .changes_excel import apply
        rows=[{'row':2,'values':['','Указание','2026-07-15','2026-07-16',str(self.obj.pk),'Секции','300 мм','200 мм','oral','ТЕСТ','','cost','']}, {'row':3,'values':['','Ошибка','','',str(self.obj.pk),'Секции','','','oral','ТЕСТ','','','']}]
        with self.assertRaises(ValidationError):apply(self.tech,self.contract.pk,rows,str(uuid.uuid4()))
        self.assertFalse(ContractChange.objects.exists())
