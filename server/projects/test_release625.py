import importlib,tempfile,uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from django.apps import apps
from django.db import connection
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import QueryDict
from django.test import TestCase,override_settings
from django.urls import reverse
from accounts.models import AccessGrant
from documents.models import Document
from documents.storage import verified_file
from finance.templatetags.finance_format import rub
from finance.forms import ReceiptForm
from repairs.construction import demand_parts
from repairs.excel import parse_standard
from repairs.models import Defect,JobDefect
from . import test_stock as base,test_release6241 as prior,agreements,rentals,repair_materials,stock_planning,equipment
from .models import CounterpartyAgreement,Contract,ContractObject,ProjectObject,EquipmentRental

class Release625Tests(TestCase):
    setUpTestData=classmethod(base.StockTests.setUpTestData.__func__)
    wh=base.StockTests.wh;data=base.StockTests.data;item=base.StockTests.item;move=base.StockTests.move;crew=base.StockTests.crew;make_job=base.StockTests.make_job;balance=base.StockTests.balance;report=base.StockTests.report
    machine=prior.Release6241Tests.machine
    def setUp(self):
        self.section.diameter_mm=720;self.section.save()
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);settings=override_settings(PRIVATE_DOCUMENT_ROOT=tmp.name);settings.enable();self.addCleanup(settings.disable);self.directory=Path(tmp.name)
    def agreement_data(self,**changes):
        return dict(number='ТЕСТ 625',kind='equipment_rent',counterparty='ТЕСТ Арендодатель',subject='Аренда экскаватора',expected_version=0,customer_contracts=[str(self.contract.pk)],project_objects=[str(self.obj.pk)],**changes)
    def other_scope(self):
        obj=ProjectObject.objects.create(legal_entity=self.company,name='ТЕСТ Второй участок');contract=Contract.objects.create(legal_entity=self.company,number='ТЕСТ 625 ВТОРОЙ',work_kinds=['repairs']);ContractObject.objects.create(project_object=obj,contract=contract);return obj,contract
    def test_p1_four_metres_is_only_two_two(self):
        self.assertEqual(demand_parts(4,'','p1')[1],[Decimal(2),Decimal(2)])
        self.assertEqual(demand_parts(4,'1.5+1.5+1','p1')[1],[Decimal(2),Decimal(2)])
    def test_rare_2500_is_one_piece(self):self.assertEqual(demand_parts('2.5','','p1')[1],[Decimal('2.5')])
    def test_catalogue_only_all_half_metre_lengths(self):
        for n in range(1000,10501,500):
            total,parts=demand_parts(Decimal(n)/1000,'','p1');self.assertEqual(sum(parts),total);self.assertTrue(all(p*1000 in (1000,1500,2000,2500) for p in parts))
        self.assertEqual(demand_parts(6,'','p1')[1],[Decimal(2)]*3)
        with self.assertRaises(ValidationError):demand_parts(11,'','p1')
        with self.assertRaises(ValidationError):demand_parts(2,'','p2')
    def test_three_four_metre_jobs_require_six_2000_pieces(self):
        for _ in range(3):
            j=self.make_job();j.method='p1';j.length_m=4;j.save()
        self.client.force_login(self.leader);response=self.client.get(reverse('stock_coverage'));rows=response.context['rows']
        self.assertEqual([(r['length'],r['need']) for r in rows],[(2000,6)])
    def test_default_reservation_requirements_are_automatic(self):
        j=self.make_job();j.method='p1';j.length_m=4;j.save();self.assertEqual(stock_planning.requirements(j),[2000,2000]);self.assertEqual(j.composition,'')
    def test_actual_alternative_is_not_rewritten_by_planning(self):
        j=self.make_job();j.method='p1';j.length_m=4;j.composition='1.5+1.5+1';j.save()
        self.assertEqual(stock_planning.requirements(j),[1500,1500,1000]);rows=repair_materials.all_demand(self.leader);self.assertEqual([(r['length'],r['need']) for r in rows[0]['couplings']],[(2000,2)]);j.refresh_from_db();self.assertEqual(j.composition,'1.5+1.5+1')
    def grinding(self,wall,method='Шлифовка'):
        headers=['Км','№ дефекта','Метод ремонта','Длина, м','Толщина стенки муфты, мм'];cells={(1,i+1):v for i,v in enumerate(headers)};cells.update({(2,i+1):v for i,v in enumerate(['1.123','625',method,'1',wall])});return parse_standard({'cells':cells,'formulas':{},'sheet':'ТЕСТ','sha256':'0'*64})
    def test_grinding_ignores_wall_even_if_non_numeric(self):
        for wall in ['8','ошибка','-1','1000000000']:
            result=self.grinding(wall);self.assertEqual(result['jobs'][0]['wall_mm'],'');self.assertEqual(result['jobs'][0]['raw'][0]['cells']['5'],wall)
    def test_coupling_wall_still_validated(self):
        with self.assertRaises(ValidationError):self.grinding('ошибка','П1')
    def test_money_display_has_no_padding_and_preserves_precision(self):
        for value,want in [('21000','21'),('150000','150'),('129000','129'),('0','0'),('21500','21,5'),('0.01','0,00001'),('-0.01','-0,00001')]:self.assertEqual(rub(Decimal(value)),want+' тыс. руб.')
    def test_money_input_converts_once_and_legacy_remains_rubles(self):
        f=ReceiptForm({'amount':'21.00001','_money_unit':'thousand','source':'ТЕСТ','reference':'625','date':str(self.today),'token':uuid.uuid4()});self.assertTrue(f.is_valid(),f.errors);self.assertEqual(f.cleaned_data['amount'],Decimal('21000.01'));self.assertEqual(str(f['amount'].value()),'21.00001')
        old=ReceiptForm({'amount':'21000.01','source':'ТЕСТ','reference':'625','date':str(self.today),'token':uuid.uuid4()});self.assertTrue(old.is_valid(),old.errors);self.assertEqual(old.cleaned_data['amount'],f.cleaned_data['amount'])
        self.assertEqual(ReceiptForm(initial={'amount':Decimal(21000)}).initial['amount'],'21')
    def test_foreman_finance_grants_never_reveal_rental_money(self):
        for key in ('finance.read','finance.write','rental.manage'):AccessGrant.objects.create(user=self.worker,permission=key,granted_by=self.leader)
        eq=self.machine();placement=equipment.assign(self.leader,eq.pk,eq.version,self.obj,self.contract,self.today,crew_assignment=self.crew());r=EquipmentRental.objects.get(placement=placement);rentals.add_rate(self.leader,r.pk,self.today,'day',Decimal(21000),Decimal(4000));rentals.save_day(self.worker,r.pk,self.today,'work')
        self.client.force_login(self.worker);response=self.client.get(reverse('equipment_detail',args=[eq.pk]));self.assertContains(response,'Наработка аренды');self.assertNotContains(response,'Взаиморасчёты');self.assertNotContains(response,'тыс. руб.');self.assertFalse(rentals.financial_reader(self.worker))
        self.assertEqual(self.client.get('/cabinet/finance/').status_code,403)
    def test_agreement_multiple_scopes_and_documents(self):
        obj,c=self.other_scope();data=self.agreement_data();data['customer_contracts'].append(str(c.pk));data['project_objects'].append(str(obj.pk));data['amount']='21';data['_money_unit']='thousand'
        row=agreements.save(self.leader,None,data,uploads=[SimpleUploadedFile('ТЕСТ договор.txt',b'private contract'),SimpleUploadedFile('ТЕСТ приложение.txt',b'annex')]);self.assertEqual(row.customer_contracts.count(),2);self.assertEqual(row.project_objects.count(),2);self.assertEqual(row.amount,21000);self.assertEqual(row.documents.count(),2)
        for doc in row.documents.all():
            self.assertEqual(doc.objects_linked.count(),2);self.assertEqual(doc.contracts_linked.count(),2)
            with verified_file(doc.current_version) as file:self.assertIn(file.read(),[b'private contract',b'annex'])
    def test_agreement_rejects_object_outside_selected_contracts(self):
        obj,c=self.other_scope();data=self.agreement_data();data['project_objects']=[str(obj.pk)]
        with self.assertRaises(ValidationError):agreements.save(self.leader,None,data)
    def test_agreement_legacy_links_are_migrated(self):
        row=CounterpartyAgreement.objects.create(number='ТЕСТ старый',kind='supply',counterparty='ТЕСТ',subject='Поставка',customer_contract=self.contract,project_object=self.obj,created_by=self.leader)
        module=importlib.import_module('projects.migrations.0028_agreement_multiple_scopes');module.copy_links(apps,SimpleNamespace(connection=connection));self.assertEqual(list(row.customer_contracts.all()),[self.contract]);self.assertEqual(list(row.project_objects.all()),[self.obj])
    def test_agreement_file_storage_rolls_back_on_version_failure(self):
        before=Document.objects.count()
        with patch('projects.agreements.make_version',side_effect=RuntimeError('ТЕСТ сбой')):
            with self.assertRaises(RuntimeError):agreements.save(self.leader,None,self.agreement_data(),uploads=[SimpleUploadedFile('x.txt',b'private')])
        self.assertEqual(Document.objects.count(),before);self.assertEqual(CounterpartyAgreement.objects.count(),0);self.assertEqual([p for p in self.directory.rglob('*') if p.is_file()],[])
    def test_new_agreement_retry_does_not_duplicate_files(self):
        data=self.agreement_data(submission_token=str(uuid.uuid4()));first=agreements.save(self.leader,None,data,uploads=[SimpleUploadedFile('x.txt',b'private')]);second=agreements.save(self.leader,None,data,uploads=[SimpleUploadedFile('x.txt',b'private')]);self.assertEqual(first.pk,second.pk);self.assertEqual(Document.objects.count(),1)
    def test_default_document_picker_does_not_dump_unrelated_files(self):
        first=agreements.save(self.leader,None,self.agreement_data(),uploads=[SimpleUploadedFile('ТЕСТ один.txt',b'one')]);other=agreements.save(self.leader,None,self.agreement_data(),uploads=[SimpleUploadedFile('ТЕСТ чужой.txt',b'two')]);form=agreements.AgreementForm(user=self.leader,instance=first);self.assertEqual(set(form.fields['documents'].queryset),set(first.documents.all()))
    def test_list_compact_preserves_excel_context(self):
        j=self.make_job();Defect.objects.create(contract=self.contract,section=self.section,kilometre=2,number='625EXTRA');j.defects.add(Defect.objects.get(number='625EXTRA'))
        self.client.force_login(self.leader);response=self.client.get(reverse('repairs'));self.assertNotContains(response,'<th scope="col">Мастер / бригада</th>');self.assertNotContains(response,'<th scope="col">Нефтепровод</th>');self.assertEqual(response.context['defect_totals']['total'],2)
        self.assertEqual(self.client.get(reverse('repair_excel_export')).status_code,200)
    def test_multiple_contract_filter_is_union(self):
        j=self.make_job();obj,c=self.other_scope();self.client.force_login(self.leader);response=self.client.get(reverse('repairs'),{'contract':[str(self.contract.pk),str(c.pk)]});self.assertEqual(response.context['page'].paginator.count,1)

    def test_agreement_human_excel_preserves_all_relationships(self):
        from repairs.importer import read_book
        from django.core.files.uploadedfile import SimpleUploadedFile
        obj,c=self.other_scope();data=self.agreement_data();data['customer_contracts'].append(str(c.pk));data['project_objects'].append(str(obj.pk));row=agreements.save(self.leader,None,data)
        self.client.force_login(self.leader);response=self.client.get(reverse('agreement_excel'),{'export':'1'})
        self.assertEqual(response.status_code,200);upload=SimpleUploadedFile('625.xlsx',response.content)
        preview=self.client.post(reverse('agreement_excel'),{'file':upload});self.assertTrue(preview.context['preview'],preview.context['form'].errors)
        apply=self.client.post(reverse('agreement_excel'),{'apply':'1','preview':preview.context['preview']});self.assertEqual(apply.status_code,302);row.refresh_from_db();self.assertEqual(row.customer_contracts.count(),2);self.assertEqual(row.project_objects.count(),2)
    def test_blank_contract_document_search_preserves_form_without_required_errors(self):
        self.client.force_login(self.leader);response=self.client.post(reverse('agreement_new'),{'_search_documents':'1','document_search':'ТЕСТ'})
        self.assertEqual(response.status_code,200);self.assertFalse(response.context['form'].errors);self.assertEqual(CounterpartyAgreement.objects.count(),0)
    def test_new_object_may_link_multiple_contracts(self):
        obj,c=self.other_scope();self.client.force_login(self.leader);response=self.client.post(reverse('object_new'),{'name':'ТЕСТ Новый общий','kind':'pipeline','status':'active','related_contract':[str(self.contract.pk),str(c.pk)]});self.assertEqual(response.status_code,302)
        created=ProjectObject.objects.get(name='ТЕСТ Новый общий');self.assertEqual(created.contracts.count(),2)

    def test_human_excel_money_is_thousands_and_import_restores_rubles(self):
        from accounts import xlsx
        from repairs.importer import read_book
        from io import BytesIO
        headers=['ID','Сумма, ₽'];raw=xlsx.workbook(self.leader,'625.money',[['','21000.01']],headers)
        book=read_book(SimpleUploadedFile('money.xlsx',raw));self.assertIn('Сумма, тыс. руб.',book['cells'].values());self.assertIn('21.00001',book['cells'].values())
        restored=xlsx.normalize(self.leader,'625.money',book,headers);self.assertEqual(Decimal(restored['cells'][(2,2)]),Decimal('21000.01'))

    def test_customer_grant_multiple_contracts_creates_only_real_pairs(self):
        from .operations import CustomerGrantForm
        from accounts.models import User
        from .operations_models import CustomerAccess
        obj,c=self.other_scope();customer=User.objects.create_user('+70000000625','123456',role='customer',full_name='ТЕСТ Заказчик',must_change_password=False)
        self.client.force_login(self.leader);response=self.client.post(reverse('customer_access'),{'user':customer.pk,'contract':[str(self.contract.pk),str(c.pk)],'objects':[str(self.obj.pk),str(obj.pk)]});self.assertEqual(response.status_code,302)
        self.assertEqual(CustomerAccess.objects.filter(user=customer).count(),2);self.assertFalse(CustomerAccess.objects.filter(user=customer,contract=c,project_object=self.obj).exists())

    def test_file_of_agreement_without_customer_scope_is_not_unclassified(self):
        from documents.access import unsorted_documents
        data=self.agreement_data();data['customer_contracts']=[];data['project_objects']=[]
        row=agreements.save(self.leader,None,data,uploads=[SimpleUploadedFile('ТЕСТ договор.txt',b'private')]);doc=row.documents.get();self.assertFalse(unsorted_documents(self.leader).filter(pk=doc.pk).exists())

    def test_multi_filter_new_work_does_not_prefill_a_comma_as_one_id(self):
        obj,c=self.other_scope();self.make_job();self.client.force_login(self.leader);response=self.client.get(reverse('repairs'),{'contract':[str(self.contract.pk),str(c.pk)],'object':[str(self.obj.pk),str(obj.pk)]})
        self.assertEqual(response.context['new_contract'],'');self.assertEqual(response.context['new_object'],'');self.assertContains(response,'contract=&amp;section=')
    def test_unknown_money_unit_is_not_silently_saved_as_rubles(self):
        form=ReceiptForm({'amount':'21','_money_unit':'unknown','source':'ТЕСТ','reference':'625','date':str(self.today),'token':uuid.uuid4()});self.assertFalse(form.is_valid());self.assertIn('_money_unit',form.errors)

    def test_shared_area_keeps_contract_context_without_wide_columns(self):
        obj,c=self.other_scope();ContractObject.objects.create(project_object=self.obj,contract=c);first=self.make_job();second=self.make_job();second.contract=c;second.save();self.client.force_login(self.leader)
        response=self.client.get(reverse('repairs'));self.assertFalse(response.context['register_single_scope']);self.assertContains(response,c.number);self.assertNotContains(response,'<th scope="col">Мастер / бригада</th>')
