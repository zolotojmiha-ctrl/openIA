import io,uuid
from decimal import Decimal
from datetime import date
from django.test import TestCase
from django.core import signing
from django.core.exceptions import ValidationError,PermissionDenied
from django.urls import reverse
from accounts.models import User,AccessGrant,AuditEvent
from documents.models import Person
from repairs.excel import workbook
from repairs.importer import read_book
from .models import (LegalEntity,ProjectObject,Contract,ContractObject,ObjectAccess,ContractAccess,RegistryImportReceipt,COMPANY_TAX_ID)
from .registry_excel import (headers,export_rows,parse_rows,apply_rows,commit_preview,HEADERS,SALT)

class RegistryExcelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.leader=User.objects.create_user('+70000000701','test-secret',full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        cls.tech=User.objects.create_user('+70000000702','test-secret',full_name='ТЕСТ Техник',role='technician',must_change_password=False)
        AccessGrant.objects.create(user=cls.tech,permission='projects.manage',granted_by=cls.leader)
        cls.entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
        cls.obj=ProjectObject.objects.create(legal_entity=cls.entity,name='ТЕСТ участок')
        cls.hidden=ProjectObject.objects.create(legal_entity=cls.entity,name='Закрытый объект')
        ObjectAccess.objects.create(user=cls.tech,project_object=cls.obj,granted_by=cls.leader)
        cls.contract=Contract.objects.create(legal_entity=cls.entity,number='Закрытый номер',subject='Закрытый предмет',counterparty='Скрытый контрагент',amount=Decimal('123456.78'),private_notes='Внутренняя тайна',work_kinds=['repairs'])
        ContractObject.objects.create(contract=cls.contract,project_object=cls.obj)
        cls.rule=ContractAccess.objects.create(contract=cls.contract,user=cls.tech,granted_by=cls.leader)
    def upload(self,rows,kind,user=None):
        f=io.BytesIO(workbook(rows,headers=headers(user or self.leader,kind)));f.name='test.xlsx';return f
    def new_object(self,name='Новый объект'):return ['',name,'Нефтепровод','Омск','В работе','2026-01-01','2026-12-31','']
    def preview(self,rows,kind,user=None):return parse_rows(self.upload(rows,kind,user),kind,user or self.leader)
    def payload(self,rows,kind,user=None):
        user=user or self.leader
        return {'actor':str(user.pk),'session':user.session_version,'kind':kind,'rows':rows,'token':str(uuid.uuid4())}
    def test_preview_is_rolled_back_then_idempotent_apply(self):
        count=AuditEvent.objects.count();rows=self.preview([self.new_object()],'objects')
        self.assertFalse(ProjectObject.objects.filter(name='Новый объект').exists());self.assertEqual(AuditEvent.objects.count(),count)
        payload=self.payload(rows,'objects');commit_preview(self.leader,'objects',payload);commit_preview(self.leader,'objects',payload)
        self.assertEqual(ProjectObject.objects.filter(name='Новый объект').count(),1);self.assertEqual(RegistryImportReceipt.objects.count(),1)
    def test_one_invalid_later_row_rolls_back_everything(self):
        rows=[{'row':2,'values':self.new_object()},{'row':3,'values':self.new_object('Ошибка')}];rows[1]['values'][6]='2025-01-01'
        with self.assertRaisesMessage(ValidationError,'Строка 3'):apply_rows(self.leader,'objects',rows)
        self.assertFalse(ProjectObject.objects.filter(name='Новый объект').exists())
    def test_stale_version_at_apply_keeps_intervening_change(self):
        values=export_rows(self.leader,'objects')[0];values[1]='Правка из Excel';rows=self.preview([values],'objects')
        target=ProjectObject.objects.get(pk=values[0]);target.name='Правка в программе';target.version+=1;target.save()
        with self.assertRaisesMessage(ValidationError,'изменилась'):apply_rows(self.leader,'objects',rows)
        target.refresh_from_db();self.assertEqual(target.name,'Правка в программе')
    def test_delegate_export_hides_private_fields_and_objects(self):
        rows=export_rows(self.tech,'contracts');self.assertEqual(len(rows),1)
        flat=str(rows);self.assertNotIn('Закрытый номер',flat);self.assertNotIn('123456',flat);self.assertNotIn('Внутренняя тайна',flat)
        self.assertNotIn(str(self.hidden.pk),str(export_rows(self.tech,'objects')))
        self.assertNotIn('Сумма, ₽',headers(self.tech,'contracts'))
    def test_delegate_round_trip_preserves_hidden_contract_fields(self):
        values=export_rows(self.tech,'contracts')[0];values[7]='В работе'
        rows=self.preview([values],'contracts',self.tech);apply_rows(self.tech,'contracts',rows)
        self.contract.refresh_from_db();self.assertEqual(self.contract.status,'active');self.assertEqual(self.contract.number,'Закрытый номер');self.assertEqual(self.contract.subject,'Закрытый предмет');self.assertEqual(self.contract.amount,Decimal('123456.78'))
    def test_delegate_cannot_write_hidden_field_or_remove_unseen_place(self):
        values=export_rows(self.tech,'contracts')[0];values[1]='Подмена'
        with self.assertRaisesMessage(ValidationError,'недоступных'):self.preview([values],'contracts',self.tech)
        ContractObject.objects.create(contract=self.contract,project_object=self.hidden)
        values=export_rows(self.tech,'contracts')[0]
        self.assertNotIn(str(self.hidden.pk),str(values))
        with self.assertRaisesMessage(ValidationError,'всем связанным'):self.preview([values],'contracts',self.tech)
    def test_new_delegate_objects_and_contracts_grant_only_creator_scope(self):
        rows=self.preview([self.new_object()],'objects',self.tech);apply_rows(self.tech,'objects',rows)
        obj=ProjectObject.objects.get(name='Новый объект');self.assertTrue(obj.assignments.filter(user=self.tech).exists())
        values=['','ТЕСТ-НОВЫЙ','24.10.2026','СМР','Другие СМР','','','Подготовка',str(obj.pk),'']
        rows=self.preview([values],'contracts',self.tech);apply_rows(self.tech,'contracts',rows)
        contract=Contract.objects.get(number='ТЕСТ-НОВЫЙ');self.assertEqual(contract.work_kinds,['construction']);self.assertTrue(contract.access_rules.filter(user=self.tech,show_number=True).exists());self.assertEqual(contract.objects_linked.get(),obj)
    def test_person_round_trip_does_not_change_account_role_or_link(self):
        AccessGrant.objects.create(user=self.tech,permission='personnel.manage',granted_by=self.leader)
        person=Person.objects.create(legal_entity=self.entity,full_name='ТЕСТ человек',position='Техник',user=self.tech,created_by=self.leader)
        values=export_rows(self.tech,'people')[0];values[1]='ТЕСТ Новое ФИО';values[2]='Инженер по ОТ и ПБ'
        rows=self.preview([values],'people',self.tech);apply_rows(self.tech,'people',rows)
        person.refresh_from_db();self.tech.refresh_from_db();self.assertEqual(person.full_name,'ТЕСТ Новое ФИО');self.assertEqual(person.user,self.tech);self.assertEqual(self.tech.role,'technician');self.assertEqual(self.tech.full_name,'ТЕСТ Техник')
    def test_person_archive_flag_is_reference_only(self):
        with self.assertRaisesMessage(ValidationError,'Архив'):self.preview([['','ТЕСТ','Мастер','1','']],'people')
    def test_contract_archiving_cannot_bypass_closure(self):
        values=export_rows(self.leader,'contracts')[0];values[7]='В архиве'
        with self.assertRaises(ValidationError):self.preview([values],'contracts')
        self.contract.refresh_from_db();self.assertNotEqual(self.contract.status,'archived')
    def test_permission_revoked_and_other_actor_preview_rejected(self):
        rows=self.preview([self.new_object()],'objects',self.tech);payload=self.payload(rows,'objects',self.tech)
        with self.assertRaises(PermissionDenied):commit_preview(self.leader,'objects',payload)
        AccessGrant.objects.filter(user=self.tech,permission='projects.manage').delete()
        with self.assertRaises(PermissionDenied):commit_preview(self.tech,'objects',payload)
        self.assertFalse(ProjectObject.objects.filter(name='Новый объект').exists())
    def test_preview_binding_and_retry_http(self):
        self.client.force_login(self.leader)
        response=self.client.post(reverse('registry_import',args=['objects']),{'file':self.upload([self.new_object()],'objects')})
        self.assertEqual(response.status_code,200);token=response.context['preview'];self.assertTrue(token)
        for _ in range(2):self.assertEqual(self.client.post(reverse('registry_import',args=['objects']),{'apply':'1','preview':token}).status_code,302)
        self.assertEqual(ProjectObject.objects.filter(name='Новый объект').count(),1)
        self.assertEqual(self.client.post(reverse('registry_import',args=['people']),{'apply':'1','preview':token}).status_code,403)
    def test_duplicate_and_unknown_ids_do_not_create_records(self):
        values=export_rows(self.leader,'objects')[0]
        with self.assertRaisesMessage(ValidationError,'повторяется'):self.preview([values,values],'objects')
        values[0]=str(uuid.uuid4())
        with self.assertRaisesMessage(ValidationError,'недоступна'):self.preview([values],'objects')
    def test_excel_date_and_decimal_amount(self):
        values=export_rows(self.leader,'contracts')[0];values[2]='46046';values[10]='1 234,50'
        rows=self.preview([values],'contracts');apply_rows(self.leader,'contracts',rows)
        self.contract.refresh_from_db();self.assertEqual(self.contract.amount,Decimal('1234.50'));self.assertIsInstance(self.contract.date,date)
    def test_export_uses_text_not_executable_formulas(self):
        self.obj.name='=HYPERLINK("https://example.invalid")';self.obj.save()
        self.client.force_login(self.leader);response=self.client.get(reverse('registry_export',args=['objects']))
        f=io.BytesIO(response.content);f.name='test.xlsx';book=read_book(f);self.assertEqual(book['sheet'],'Объекты');self.assertFalse(book['formulas']);self.assertIn(self.obj.name,book['cells'].values())
    def test_round_trip_audit_contains_previous_values(self):
        values=export_rows(self.leader,'objects')[0];original=values[1];values[1]='Новое название'
        apply_rows(self.leader,'objects',self.preview([values],'objects'))
        event=AuditEvent.objects.filter(action='object_updated').latest('pk')
        self.assertEqual(event.details['before']['name'],original)
    def test_unknown_kind_and_unauthorized_routes(self):
        self.client.force_login(self.tech)
        self.assertEqual(self.client.get(reverse('registry_export',args=['unknown'])).status_code,404)
        self.assertEqual(self.client.get(reverse('registry_export',args=['people'])).status_code,403)

    def test_import_cannot_detach_subcontract_place_or_work_kind(self):
        from .models import Subcontractor,SubcontractOrder
        ContractObject.objects.create(contract=self.contract,project_object=self.hidden)
        company=Subcontractor.objects.create(legal_entity=self.entity,name='ТЕСТ компания',tax_id='0000000000',created_by=self.leader)
        SubcontractOrder.objects.create(company=company,contract=self.contract,project_object=self.obj,kind='repairs',title='ТЕСТ поручение',created_by=self.leader)
        values=export_rows(self.leader,'contracts')[0];values[8]=str(self.hidden.pk)
        with self.assertRaisesMessage(ValidationError,'Нельзя убрать объект'):self.preview([values],'contracts')
        values[8]=str(self.obj.pk)+';'+str(self.hidden.pk);values[4]='Замена запорной арматуры'
        with self.assertRaisesMessage(ValidationError,'вид работ'):self.preview([values],'contracts')
        self.assertEqual(self.contract.objects_linked.count(),2)

    def test_closed_contract_is_exportable_but_not_editable(self):
        from .archive import lifecycle_write
        with lifecycle_write():Contract.objects.filter(pk=self.contract.pk).update(status='archived')
        self.assertEqual(export_rows(self.leader,'contracts'),[])
        rows=export_rows(self.leader,'contracts',archived=True);self.assertEqual(len(rows),1)
        with self.assertRaisesMessage(ValidationError,'в архиве'):self.preview(rows,'contracts')
