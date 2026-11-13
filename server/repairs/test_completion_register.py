import io,zipfile
from datetime import timedelta
from decimal import Decimal
from xml.etree import ElementTree as ET
from django.test import TestCase
from django.urls import reverse
from accounts.models import User
from projects.models import ObjectAccess,ContractAccess,ProjectObject,Contract,ContractObject
from projects import test_stock as fixture
from .models import Report,Defect,JobDefect,Job,Section
from . import completion_register as service

class CompletionRegisterTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    make_job=fixture.StockTests.make_job
    def setUp(self):
        self.pto=User.objects.create_user('+70000000004',role='pto',full_name='ТЕСТ ПТО',must_change_password=False)
        ObjectAccess.objects.create(user=self.pto,project_object=self.obj,granted_by=self.leader)
        ContractAccess.objects.create(user=self.pto,contract=self.contract,granted_by=self.leader)
        self.job=self.make_job();self.job.state='confirmed';self.job.accepted_date=self.today;self.job.acceptance_basis='ТЕСТ акт';self.job.save()
        self.report=Report.objects.create(job=self.job,author=self.worker,kind='complete',performed_date=self.today-timedelta(days=2),method='p1',length_m=Decimal('3'),composition='1,5 + 1,5',context={'actual_coating_length_m':'3.8'},confirmed_by=self.leader)
        self.client.force_login(self.pto);self.url=reverse('repair_completed')
    def test_pto_reads_all_facts_without_opening_card_and_without_mutation(self):
        before=Report.objects.count();response=self.client.get(self.url)
        self.assertEqual(response.status_code,200)
        for text in ('3,8','1,5 + 1,5','ТЕСТ акт',self.worker.full_name,self.pto.full_name,self.report.performed_date.strftime('%d.%m.%Y')):
            if text!=self.pto.full_name:self.assertContains(response,text)
        group=response.context['groups'][0]
        self.assertEqual(group['report'].method,'p1');self.assertEqual(group['report'].length_m,3)
        self.assertEqual(Report.objects.count(),before);self.job.refresh_from_db();self.assertEqual(self.job.state,'confirmed')
        self.assertNotContains(response,'Подтвердить устранение')
    def test_date_filter_uses_last_actual_result_not_plan_or_photo_date(self):
        self.job.planned_date=self.today;self.job.save()
        Report.objects.create(job=self.job,author=self.worker,kind='note',performed_date=self.today)
        self.assertEqual(self.client.get(self.url,{'start':str(self.today)}).context['summary']['jobs'],0)
        self.assertEqual(self.client.get(self.url,{'end':str(self.today-timedelta(days=1))}).context['summary']['jobs'],1)
        latest=Report.objects.create(job=self.job,author=self.leader,kind='correction',performed_date=self.today,method='p2',length_m=2,context={'actual_coating_length_m':'2.5'})
        response=self.client.get(self.url,{'start':str(self.today)})
        self.assertEqual(response.context['groups'][0]['report'],latest);self.assertEqual(response.context['summary']['coating'],Decimal('2.5'))
    def test_common_construction_counts_once_and_export_has_one_numeric_measurement(self):
        d=Defect.objects.create(contract=self.contract,section=self.section,kilometre=2,number='ТЕСТ второй',pipe_section='099')
        JobDefect.objects.create(job=self.job,defect=d)
        response=self.client.get(self.url);stats=response.context['summary']
        self.assertEqual((stats['defects'],stats['jobs'],stats['coating']),(2,1,Decimal('3.8')))
        response=self.client.get(self.url,{'download':'xlsx'})
        with zipfile.ZipFile(io.BytesIO(response.content)) as book:
            sheet=ET.fromstring(book.read('xl/worksheets/sheet1.xml'))
        ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        numeric=sheet.findall('.//s:c[@t="n"]/s:v',ns)
        self.assertEqual([Decimal(c.text) for c in numeric],[Decimal('3'),Decimal('3.8')])
        self.assertIn('ТЕСТ второй',' '.join(sheet.itertext()))
        self.assertEqual(len(sheet.findall('.//s:row',ns)),3)
        self.assertEqual(sheet.find('s:autoFilter',ns).attrib['ref'],'A1:Q3')
    def test_legacy_missing_measurement_remains_unknown_and_not_planned_length(self):
        self.report.context={};self.report.length_m=None;self.report.save()
        response=self.client.get(self.url)
        self.assertEqual(response.context['summary']['missing'],1);self.assertIsNone(response.context['groups'][0]['coating'])
        self.assertContains(response,'Не указан');self.assertEqual(response.context['summary']['coating'],0)
    def test_only_currently_completed_results_and_state_master_filters(self):
        other=self.make_job();Report.objects.create(job=other,author=self.worker,kind='complete',performed_date=self.today)
        self.assertEqual(self.client.get(self.url).context['summary']['jobs'],1)
        self.assertEqual(self.client.get(self.url,{'state':'reported'}).context['summary']['jobs'],0)
        self.assertEqual(self.client.get(self.url,{'master':str(self.worker.pk)}).context['summary']['jobs'],1)
        self.job.state='progress';self.job.save()
        self.assertEqual(self.client.get(self.url).context['summary']['jobs'],0)
    def test_object_and_contract_scope_apply_to_screen_export_and_choices(self):
        area=ProjectObject.objects.create(legal_entity=self.company,name='ТЕСТ секретный объект')
        contract=Contract.objects.create(legal_entity=self.company,number='ТЕСТ секретный контракт')
        ContractObject.objects.create(project_object=area,contract=contract)
        section=Section.objects.create(project_object=area,rnu='ТЕСТ',pipeline='ТЕСТ',name='ТЕСТ секретный участок')
        other=self.make_job();other.section=section;other.contract=contract;other.state='confirmed';other.save()
        # Move the linked synthetic defect consistently without changing permissions.
        defect=other.defects.get();defect.contract=contract;defect.section=section;defect.save()
        Report.objects.create(job=other,author=self.worker,kind='complete',performed_date=self.today,method='p2',context={'actual_coating_length_m':'999'})
        response=self.client.get(self.url);self.assertNotContains(response,'секретный');self.assertEqual(response.context['summary']['jobs'],1)
        for key,value in [('object',area.pk),('contract',contract.pk),('section',section.pk)]:
            response=self.client.get(self.url,{key:str(value),'download':'xlsx'});self.assertEqual(response.status_code,400)
        ContractAccess.objects.filter(user=self.pto).delete()
        self.assertEqual(self.client.get(self.url).context['summary']['jobs'],0)
    def test_invalid_filters_show_errors_and_export_refuses_instead_of_returning_all(self):
        for data in ({'start':'wrong'},{'start':str(self.today),'end':str(self.today-timedelta(days=1))},{'state':'planned'},{'master':'wrong'}):
            response=self.client.get(self.url,data);self.assertFalse(response.context['form'].is_valid());self.assertEqual(response.context['groups'],[])
            self.assertEqual(self.client.get(self.url,{**data,'download':'xlsx'}).status_code,400)
    def test_bulk_scope_from_existing_register_and_search_keeps_common_work(self):
        response=self.client.get(self.url,{'contract':str(self.contract.pk),'object':str(self.obj.pk),'q':self.job.active_defects[0].number})
        self.assertEqual(response.context['summary']['jobs'],1)
        self.assertContains(response,'download=xlsx')
    def test_totals_and_export_cover_all_pages_and_keep_constructions_whole(self):
        for _ in range(25):
            job=self.make_job();job.state='confirmed';job.save()
            Report.objects.create(job=job,author=self.worker,kind='complete',performed_date=self.today,
                                  method='grinding',context={'actual_coating_length_m':'1'})
        response=self.client.get(self.url,{'page':'2'})
        self.assertEqual(response.context['summary']['jobs'],26)
        self.assertEqual(response.context['summary']['coating'],Decimal('28.8'))
        self.assertEqual(len(response.context['groups']),1)
        response=self.client.get(self.url,{'page':'2','download':'xlsx'})
        with zipfile.ZipFile(io.BytesIO(response.content)) as book:
            sheet=ET.fromstring(book.read('xl/worksheets/sheet1.xml'))
        self.assertEqual(len(sheet.findall('{*}sheetData/{*}row')),27)

    def test_external_and_foreman_roles_cannot_use_internal_register(self):
        for role in ('customer','subcontractor','foreman','mechanic'):
            self.pto.role=role;self.pto.save(update_fields=['role']);self.client.force_login(self.pto)
            self.assertEqual(self.client.get(self.url).status_code,403)
            self.assertEqual(self.client.get(self.url,{'download':'xlsx'}).status_code,403)
    def test_contract_entry_includes_all_accessible_sections_and_no_longer_in_each_place(self):
        area=ProjectObject.objects.create(legal_entity=self.company,name='ТЕСТ второй участок')
        ContractObject.objects.create(contract=self.contract,project_object=area)
        ObjectAccess.objects.create(user=self.pto,project_object=area,granted_by=self.leader)
        section=Section.objects.create(project_object=area,rnu='ТЕСТ',pipeline='ТЕСТ',name='ТЕСТ второй')
        job=Job.objects.create(contract=self.contract,section=section,method='grind',state='confirmed',created_by=self.leader)
        defect=Defect.objects.create(contract=self.contract,section=section,kilometre=3,number='ТЕСТ второй участок')
        JobDefect.objects.create(job=job,defect=defect)
        Report.objects.create(job=job,author=self.worker,kind='complete',performed_date=self.today,method='grind')
        page=self.client.get(reverse('work_contract',args=[self.contract.pk]))
        self.assertContains(page,self.url+'?contract='+str(self.contract.pk))
        self.assertContains(page,'Ведомость выполнения по контракту')
        report=self.client.get(self.url,{'contract':str(self.contract.pk)})
        self.assertEqual(report.context['summary']['jobs'],2)
        place=self.client.get(reverse('work_place',args=[self.contract.pk,self.obj.pk]))
        self.assertNotContains(place,'Ведомость выполнения')

    def test_link_in_existing_register_and_card_return_keeps_filters(self):
        response=self.client.get(reverse('repairs'));self.assertContains(response,self.url)
        path=self.url+'?start='+str(self.today-timedelta(days=3))
        response=self.client.get(reverse('repair_detail',args=[self.job.pk]),{'return_to':path})
        self.assertEqual(response.context['back'],path)
