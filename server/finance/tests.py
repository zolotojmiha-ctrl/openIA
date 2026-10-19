import io
import secrets
import tempfile
import shutil
import zipfile
from decimal import Decimal as D
from xml.sax.saxutils import escape
from datetime import date,timedelta
from uuid import uuid4
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase,Client,override_settings
from django.urls import reverse
from django.utils import timezone
from accounts.models import User,AccessGrant,AuditEvent
from projects.models import LegalEntity,COMPANY_TAX_ID
from .models import FinancialScope,OpeningSnapshot,FinancialImport,CashReceipt,PaymentAllocation,WorkExecution,AdvanceOffset,CostEntry
from .services import summary
from .importer import parse_report

# Synthetic amounts only. Never embed the user's financial spreadsheet in tests.
def report_file(mismatch=False):
    pairs=[('ТЕСТ. 10 дефектов',None),('Устранено, ед.:',4),('Получен аванс',500),('Закрыто по КС',800),('Получили',600),('Зачтено аванса',200),('Остаток аванса',310 if mismatch else 300),('Выполнено по факту, но не оплачено:',None),('Учебное выполнение. Формирование ИД',100),('Учебная корректировка',23),('Итого не выплачено',123)]
    rows=[]
    for i,(label,value) in enumerate(pairs,1):
        cells=f'<c r="A{i}" t="inlineStr"><is><t>{escape(label)}</t></is></c>'
        if value is not None:cells+=f'<c r="B{i}"><v>{value}</v></c>'
        rows.append(f'<row r="{i}">{cells}</row>')
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:z.writestr(zipfile.ZipInfo('xl/worksheets/sheet1.xml',date_time=(2020,1,1,0,0,0)),'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join(rows)+'</sheetData></worksheet>')
    return SimpleUploadedFile('synthetic-finance.xlsx',stream.getvalue())

class FinanceTests(TestCase):
    def setUp(self):
        self.private_root=tempfile.mkdtemp(prefix='armada-finance-files-')
        self.private_override=override_settings(PRIVATE_DOCUMENT_ROOT=self.private_root);self.private_override.enable();self.addCleanup(self.private_override.disable);self.addCleanup(shutil.rmtree,self.private_root)
        self.leader=User.objects.create_user('+70000000001',secrets.token_urlsafe(24),full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        self.employee=User.objects.create_user('+70000000003',secrets.token_urlsafe(24),full_name='ТЕСТ Бухгалтер',role='accounting',must_change_password=False)
        self.company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ Компания'})[0]
        self.scope=FinancialScope.objects.create(legal_entity=self.company,title='ТЕСТ направление',created_by=self.leader)
        self.client.force_login(self.leader)
    def receipt(self,amount=1000):return CashReceipt.objects.create(legal_entity=self.company,token=uuid4(),date=date.today(),amount=amount,source='ТЕСТ счёт',reference='ТЕСТ платёж',created_by=self.leader)
    def work(self,amount=1000,accepted=True,due=None):return WorkExecution.objects.create(scope=self.scope,title='ТЕСТ выполнение',date=date.today(),factual_amount=amount,accepted_amount=amount if accepted else 0,accepted_by=self.leader if accepted else None,created_by=self.leader,payment_due=due)
    def test_parser_keeps_source_values_and_warns_without_changing_them(self):
        data=parse_report(report_file(True));b=data['blocks'][0]
        self.assertEqual(b['advance_difference'],D('10'));self.assertEqual(b['declared_advance_balance'],D('310'))
        self.assertEqual(b['declared_pending'],D('123'));self.assertEqual(len(b['lines']),2)
    def test_import_preview_then_commit_and_idempotency(self):
        url=reverse('finance_import')
        self.assertContains(self.client.post(url,{'file':report_file()}),'123,00')
        self.assertFalse(FinancialImport.objects.exists())
        self.assertEqual(self.client.post(url,{'file':report_file(),'confirm':'on'}).status_code,302)
        scope=FinancialScope.objects.get(title='ТЕСТ. 10 дефектов');card=summary(scope)
        self.assertEqual(card['accepted_unpaid'],0);self.assertEqual(card['pending'],123);self.assertEqual(card['progress'],40)
        self.assertContains(self.client.post(url,{'file':report_file(),'confirm':'on'}),'уже загружена')
        self.assertEqual(FinancialImport.objects.count(),1)
    def test_financial_views_not_visible_to_employee_without_explicit_grant(self):
        self.client.force_login(self.employee)
        for name,args in [('finance',[]),('finance_detail',[self.scope.pk]),('finance_import',[])]:self.assertEqual(self.client.get(reverse(name,args=args)).status_code,403)
        self.assertNotContains(self.client.get(reverse('dashboard')),self.scope.title)
    def test_reader_cannot_write(self):
        AccessGrant.objects.create(user=self.employee,permission='finance.read',granted_by=self.leader)
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse('finance')).status_code,200)
        self.assertEqual(self.client.get(reverse('finance_create',args=['receipt'])).status_code,403)
    def test_payment_allocation_cap_and_split(self):
        receipt=self.receipt();url=reverse('finance_allocate',args=[receipt.pk])
        self.assertEqual(self.client.post(url,{'scope':self.scope.pk,'purpose':'advance','amount':600}).status_code,302)
        response=self.client.post(url,{'scope':self.scope.pk,'purpose':'work','amount':500})
        self.assertContains(response,'превышает остаток');self.assertEqual(PaymentAllocation.objects.count(),1)
        self.assertEqual(summary(self.scope)['advances'],600)
    def test_offset_does_not_create_second_cash_receipt(self):
        receipt=self.receipt();PaymentAllocation.objects.create(receipt=receipt,scope=self.scope,purpose='advance',amount=1000,created_by=self.leader)
        work=self.work(800)
        response=self.client.post(reverse('finance_create',args=['offset']),{'work':work.pk,'amount':300,'date':date.today(),'basis':'ТЕСТ условие'})
        self.assertEqual(response.status_code,302)
        card=summary(self.scope);self.assertEqual(card['advances'],1000);self.assertEqual(card['offsets'],300);self.assertEqual(card['expected_advance_balance'],700);self.assertEqual(card['accepted_unpaid'],500)
        self.assertEqual(CashReceipt.objects.count(),1)
    def test_offset_cannot_exceed_advance_or_work(self):
        work=self.work(800)
        response=self.client.post(reverse('finance_create',args=['offset']),{'work':work.pk,'amount':1,'date':date.today(),'basis':'ТЕСТ условие'})
        self.assertContains(response,'больше расчётного');self.assertFalse(AdvanceOffset.objects.exists())
    def test_receivable_due_dates_separate_from_unclosed_work(self):
        self.work(100,due=timezone.localdate()-timedelta(days=1));self.work(200,due=timezone.localdate()+timedelta(days=1));self.work(300);self.work(400,accepted=False)
        card=summary(self.scope);self.assertEqual(card['due_overdue'],100);self.assertEqual(card['due_future'],200);self.assertEqual(card['due_unknown'],300);self.assertEqual(card['accepted'],600)
    def test_only_leader_confirms_accepted_work(self):
        work=self.work(500,accepted=False)
        AccessGrant.objects.create(user=self.employee,permission='finance.write',granted_by=self.leader);self.client.force_login(self.employee)
        url=reverse('finance_accept',args=[work.pk]);self.assertEqual(self.client.get(url).status_code,403)
        self.client.force_login(self.leader);data={'expected_revision':1,'accepted_amount':500,'basis':'ТЕСТ КС'}
        self.assertEqual(self.client.post(url,data).status_code,302);self.assertEqual(self.client.post(url,data).status_code,409)
    def test_shared_costs_not_distributed(self):
        CostEntry.objects.create(legal_entity=self.company,date=date.today(),kind='overhead',amount=100,paid_amount=100,basis='ТЕСТ общие затраты',created_by=self.leader)
        self.assertEqual(summary(self.scope)['overhead'],0);self.assertIsNone(summary(self.scope)['result'])
    def test_csrf_enforced_for_finance_changes(self):
        client=Client(enforce_csrf_checks=True);client.force_login(self.leader)
        self.assertEqual(client.post(reverse('finance_import'),{'file':report_file(),'confirm':'on'}).status_code,403)
    def test_foreign_entity_scope_is_not_accessible(self):
        other=LegalEntity.objects.create(tax_id='0000000000',name='ТЕСТ другая компания')
        scope=FinancialScope.objects.create(legal_entity=other,title='ЧУЖИЕ ФИНАНСЫ',created_by=self.leader)
        self.assertEqual(self.client.get(reverse('finance_detail',args=[scope.pk])).status_code,404)
        self.assertNotContains(self.client.get(reverse('finance')),scope.title)
    def test_reverse_allocation_restores_available_cash_without_deleting_history(self):
        receipt=self.receipt();item=PaymentAllocation.objects.create(receipt=receipt,scope=self.scope,purpose='advance',amount=500,created_by=self.leader)
        self.assertEqual(self.client.post(reverse('finance_reverse',args=[item.pk]),{'reason':'ТЕСТ ошибка назначения'}).status_code,302)
        item.refresh_from_db();self.assertTrue(item.reversed);self.assertEqual(summary(self.scope)['advances'],0)
        self.assertEqual(PaymentAllocation.objects.count(),1)
    def test_existing_pending_continues_without_double_counting(self):
        from .models import PendingWork
        p=PendingWork.objects.create(scope=self.scope,title='ТЕСТ строка справки',amount=100)
        self.assertEqual(summary(self.scope)['pending'],100)
        work=self.work(100);work.source_pending=p;work.save()
        self.assertEqual(summary(self.scope)['pending'],100)
        receipt=self.receipt(50);PaymentAllocation.objects.create(receipt=receipt,scope=self.scope,purpose='work',work=work,amount=50,created_by=self.leader)
        self.assertEqual(summary(self.scope)['pending'],50)
    def test_payment_without_work_cannot_reduce_unrelated_work_debt(self):
        self.work(100);receipt=self.receipt(50)
        response=self.client.post(reverse('finance_allocate',args=[receipt.pk]),{'scope':self.scope.pk,'purpose':'work','amount':50})
        self.assertContains(response,'Выберите принятое выполнение');self.assertFalse(PaymentAllocation.objects.exists())
    def test_source_acceptance_requires_one_of_two_leaders(self):
        AccessGrant.objects.create(user=self.employee,permission='finance.write',granted_by=self.leader);self.client.force_login(self.employee)
        self.assertContains(self.client.post(reverse('finance_import'),{'file':report_file(),'confirm':'on'}),'один из двух руководителей')
        self.assertFalse(FinancialImport.objects.exists())

    def test_preview_confirm_retains_private_source_document(self):
        from .models import FinancialDraft
        from documents.models import Document
        response=self.client.post(reverse('finance_import'),{'file':report_file()})
        self.assertEqual(response.status_code,200);draft=FinancialDraft.objects.get(user=self.leader)
        self.assertIsNotNone(draft.source_document_id)
        self.assertEqual(self.client.post(reverse('finance_confirm',args=[draft.pk])).status_code,302)
        source=FinancialImport.objects.get();self.assertEqual(source.source_document_id,draft.source_document_id)
        self.assertTrue(Document.objects.filter(pk=source.source_document_id).exists())
    def test_wrong_user_cannot_confirm_private_draft(self):
        from .models import FinancialDraft
        self.client.post(reverse('finance_import'),{'file':report_file()});draft=FinancialDraft.objects.get()
        second=User.objects.create_user('+70000000002',secrets.token_urlsafe(24),full_name='ТЕСТ другой руководитель',leader_slot=2,must_change_password=False)
        self.client.force_login(second);self.assertEqual(self.client.post(reverse('finance_confirm',args=[draft.pk])).status_code,404)
    def test_receipt_correction_cannot_go_below_allocations(self):
        receipt=self.receipt();PaymentAllocation.objects.create(receipt=receipt,scope=self.scope,purpose='advance',amount=800,created_by=self.leader)
        url=reverse('finance_edit',args=['receipt',receipt.pk]);form=self.client.get(url).context['form']
        data={'token':receipt.token,'date':receipt.date,'amount':700,'source':receipt.source,'reference':receipt.reference,'previous':form.initial['previous'],'reason':'ТЕСТ исправление'}
        self.assertContains(self.client.post(url,data),'ниже уже распределённого');receipt.refresh_from_db();self.assertEqual(receipt.amount,1000)
    def test_offset_reversal_preserves_history_and_restores_advance(self):
        receipt=self.receipt();PaymentAllocation.objects.create(receipt=receipt,scope=self.scope,purpose='advance',amount=1000,created_by=self.leader);work=self.work(800)
        offset=AdvanceOffset.objects.create(work=work,amount=300,date=date.today(),basis='ТЕСТ',created_by=self.leader)
        self.assertEqual(self.client.post(reverse('finance_reverse_offset',args=[offset.pk]),{'reason':'ТЕСТ ошибочный зачёт'}).status_code,302)
        self.assertEqual(summary(self.scope)['expected_advance_balance'],1000);offset.refresh_from_db();self.assertTrue(offset.reversed)
    def test_repeated_allocation_operation_token_does_not_charge_twice(self):
        receipt=self.receipt();token=uuid4()
        data={'operation_token':token,'scope':self.scope.pk,'purpose':'advance','amount':400}
        url=reverse('finance_allocate',args=[receipt.pk])
        self.assertEqual(self.client.post(url,data).status_code,302);self.assertEqual(self.client.post(url,data).status_code,302)
        self.assertEqual(PaymentAllocation.objects.count(),1);self.assertEqual(summary(self.scope)['advances'],400)
    def test_repeated_cost_operation_token_does_not_duplicate_expense(self):
        data={'operation_token':uuid4(),'scope':self.scope.pk,'date':date.today(),'kind':'direct','amount':100,'paid_amount':50,'basis':'ТЕСТ затраты'}
        url=reverse('finance_create',args=['cost'])
        self.assertEqual(self.client.post(url,data).status_code,302);self.assertEqual(self.client.post(url,data).status_code,302)
        self.assertEqual(CostEntry.objects.count(),1);self.assertEqual(summary(self.scope)['direct'],100)
    def test_source_file_replacement_does_not_change_financial_basis(self):
        from .models import FinancialDraft
        from documents.models import Document
        self.client.post(reverse('finance_import'),{'file':report_file()});draft=FinancialDraft.objects.get()
        self.client.post(reverse('finance_confirm',args=[draft.pk]));source=FinancialImport.objects.get()
        old_version=source.source_version_id;doc=source.source_document
        response=self.client.post(reverse('document_replace',args=[doc.pk]),{'expected_revision':doc.revision,'file':SimpleUploadedFile('new-source.txt',b'new independent document')})
        self.assertEqual(response.status_code,302);source.refresh_from_db();doc.refresh_from_db()
        self.assertEqual(source.source_version_id,old_version);self.assertNotEqual(doc.current_version_id,old_version)
        self.assertEqual(summary(FinancialScope.objects.get(title='ТЕСТ. 10 дефектов'))['accepted'],800)
