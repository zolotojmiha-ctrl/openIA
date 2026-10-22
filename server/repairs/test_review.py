import secrets,uuid
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.core import signing
from django.core.exceptions import ValidationError,PermissionDenied
from django.utils import timezone
from unittest.mock import patch
from accounts.models import User,AuditEvent
from projects.models import LegalEntity,ProjectObject,Contract,ContractObject,ObjectAccess,ContractAccess,COMPANY_TAX_ID
from .models import Job,Defect,JobDefect,Section,Report,Attachment
from .review_services import confirm_many,return_for_revision,correct_and_accept
from .review_views import ReviewCorrectionForm

class ReviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        password=secrets.token_urlsafe(24)
        cls.leader=User.objects.create_user('+70000000001',password,full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        cls.worker=User.objects.create_user('+70000000002',password,full_name='ТЕСТ Мастер',role='foreman',must_change_password=False)
        cls.company=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
        cls.obj=ProjectObject.objects.create(legal_entity=cls.company,name='ТЕСТ Объект')
        cls.contract=Contract.objects.create(legal_entity=cls.company,number='ТЕСТ-1',subject='Дефекты',work_kinds=['repairs'])
        ContractObject.objects.create(contract=cls.contract,project_object=cls.obj)
        ObjectAccess.objects.create(user=cls.worker,project_object=cls.obj,granted_by=cls.leader)
        ContractAccess.objects.create(user=cls.worker,contract=cls.contract,show_number=True,granted_by=cls.leader)
        cls.section=Section.objects.create(project_object=cls.obj,rnu='ТЕСТ',pipeline='ТЕСТ',name='ТЕСТ Участок')
    def make_job(self,number='001',state='reported',contract=None):
        contract=contract or self.contract
        j=Job.objects.create(contract=contract,section=self.section,assigned_to=self.worker,method='grind',state=state,created_by=self.leader)
        d=Defect.objects.create(contract=contract,section=self.section,kilometre=100+int(number)/1000,number=number,pipe_section='0001');JobDefect.objects.create(job=j,defect=d)
        r=Report.objects.create(job=j,author=self.worker,kind='complete',outcome='complete',performed_date=timezone.localdate(),method='grind',latitude=55,longitude=73,note='Исходный результат')
        return j,r
    def versions(self,*jobs):return {str(j.pk):j.version for j in jobs}
    def filter_token(self,**filters):return signing.dumps({'actor':str(self.leader.pk),'filters':filters},salt='repair-review-filter')
    def correction_data(self,j,**changes):return {'token':uuid.uuid4(),'expected_version':j.version,'performed_date':timezone.localdate(),'outcome':'complete','method':'grind','length_m':None,'composition':'','note':'Уточнены координаты','latitude':Decimal('55.1'),'longitude':Decimal('73.2'),'couplings':[],'files':[],**changes}
    def test_bulk_confirms_complete_reports_and_preserves_authors(self):
        a,ar=self.make_job();b,br=self.make_job('002')
        self.assertEqual(confirm_many(self.leader,self.versions(a,b)),2)
        for j,r in [(a,ar),(b,br)]:
            j.refresh_from_db();r.refresh_from_db();self.assertEqual(j.state,'confirmed');self.assertEqual(r.author,self.worker);self.assertEqual(r.confirmed_by,self.leader);self.assertEqual(j.reports.count(),1)
    def test_bulk_stale_or_nonpending_rolls_back_every_job(self):
        a,ar=self.make_job();b,br=self.make_job('002');b.version+=1;b.save()
        with self.assertRaises(ValidationError):confirm_many(self.leader,{str(a.pk):1,str(b.pk):1})
        a.refresh_from_db();ar.refresh_from_db();self.assertEqual(a.state,'reported');self.assertIsNone(ar.confirmed_by)
    def test_nonleaders_cannot_review(self):
        j,r=self.make_job()
        with self.assertRaises(PermissionDenied):confirm_many(self.worker,self.versions(j))
        with self.assertRaises(PermissionDenied):return_for_revision(self.worker,j.pk,j.version,'Причина')
        for name in ['repair_return','repair_correct_accept']:
            self.client.force_login(self.worker);self.assertEqual(self.client.get(reverse(name,args=[j.pk])).status_code,403)
    def test_all_by_filter_covers_more_than_one_page(self):
        jobs=[self.make_job(str(i).zfill(3))[0] for i in range(1,29)]
        self.client.force_login(self.leader)
        r=self.client.post(reverse('repair_bulk_review'),{'filter_token':self.filter_token(contract=str(self.contract.pk),state='reported'),'scope':'all'})
        self.assertEqual(r.status_code,200);self.assertEqual(len(r.context['jobs']),28)
        self.assertEqual(self.client.post(reverse('repair_bulk_review'),{'snapshot':r.context['snapshot'],'apply':'1'}).status_code,302)
        self.assertEqual(Job.objects.filter(state='confirmed').count(),28)
    def test_selection_uses_signed_filter_and_deduplicates_group_checkboxes(self):
        a,ar=self.make_job();b,br=self.make_job('002')
        b.defects.update(pipe_section='9999')
        self.client.force_login(self.leader)
        r=self.client.post(reverse('repair_bulk_review'),{'filter_token':self.filter_token(q='001'),'scope':'selected','jobs':[str(a.pk),str(a.pk)]})
        self.assertEqual(r.status_code,200);self.assertEqual(len(r.context['jobs']),1)
        r=self.client.post(reverse('repair_bulk_review'),{'filter_token':self.filter_token(q='001'),'scope':'selected','jobs':[str(b.pk)]});self.assertEqual(r.status_code,409)
        r=self.client.post(reverse('repair_bulk_review'),{'filter_token':'forged','scope':'all'});self.assertEqual(r.status_code,409)
    def test_new_results_after_preview_not_implicitly_added(self):
        a,ar=self.make_job();self.client.force_login(self.leader)
        r=self.client.post(reverse('repair_bulk_review'),{'filter_token':self.filter_token(),'scope':'all'});b,br=self.make_job('002')
        self.client.post(reverse('repair_bulk_review'),{'snapshot':r.context['snapshot'],'apply':'1'})
        a.refresh_from_db();b.refresh_from_db();self.assertEqual(a.state,'confirmed');self.assertEqual(b.state,'reported')
    def test_shared_construction_preview_shows_all_defects(self):
        a,ar=self.make_job();d=Defect.objects.create(contract=self.contract,section=self.section,kilometre='100.001',number='0002');JobDefect.objects.create(job=a,defect=d)
        self.client.force_login(self.leader);r=self.client.post(reverse('repair_bulk_review'),{'filter_token':self.filter_token(),'scope':'selected','jobs':[a.pk]})
        self.assertEqual(r.context['defect_count'],2);self.assertContains(r,'общий результат')
    def test_return_preserves_original_and_notifies_master(self):
        j,r=self.make_job();return_for_revision(self.leader,j.pk,j.version,'Уточните координаты')
        j.refresh_from_db();r.refresh_from_db();self.assertEqual(j.state,'progress');self.assertEqual(r.latitude,55);self.assertEqual(r.author,self.worker);self.assertEqual(j.reports.filter(kind='revision').count(),1)
        self.client.force_login(self.worker);self.assertContains(self.client.get(reverse('dashboard')),'Возвращено на доработку: 1')
        self.assertContains(self.client.get(reverse('repair_detail',args=[j.pk])),'Уточните координаты')
    def test_return_requires_note_and_current_pending_state(self):
        j,r=self.make_job()
        with self.assertRaises(ValidationError):return_for_revision(self.leader,j.pk,j.version,'  ')
        confirm_many(self.leader,self.versions(j))
        with self.assertRaises(ValidationError):return_for_revision(self.leader,j.pk,j.version,'Поздно')
    def test_new_result_clears_return_notice_without_erasing_revision(self):
        from .services import report_work
        j,r=self.make_job();return_for_revision(self.leader,j.pk,j.version,'Повторите');j.refresh_from_db()
        data=self.correction_data(j);report_work(self.worker,j.pk,'result',data);j.refresh_from_db();self.assertEqual(j.review_note,'');self.assertEqual(j.reports.filter(kind='revision').count(),1)
    def test_correct_accept_preserves_original_and_is_retry_safe(self):
        j,r=self.make_job();data=self.correction_data(j)
        correct_and_accept(self.leader,j.pk,data);correct_and_accept(self.leader,j.pk,data)
        j.refresh_from_db();r.refresh_from_db();self.assertEqual(j.state,'confirmed');self.assertEqual(r.latitude,55);self.assertEqual(r.author,self.worker)
        correction=j.reports.get(kind='correction');self.assertEqual(correction.latitude,Decimal('55.1'));self.assertEqual(correction.confirmed_by,self.leader);self.assertEqual(correction.context['corrected_report'],str(r.pk));self.assertEqual(j.reports.count(),2)
    def test_correct_accept_failure_rolls_back_edit(self):
        j,r=self.make_job()
        with patch('repairs.review_services.confirm_work',side_effect=ValidationError('Конфликт')):
            with self.assertRaises(ValidationError):correct_and_accept(self.leader,j.pk,self.correction_data(j))
        j.refresh_from_db();self.assertEqual(j.state,'reported');self.assertEqual(j.reports.count(),1)
    def test_correction_form_prefills_coordinates_and_requires_reason(self):
        j,r=self.make_job();f=ReviewCorrectionForm(job=j,user=self.leader)
        self.assertEqual(f.initial['coordinates'],'55.000000, 73.000000')
        data={'token':uuid.uuid4(),'expected_version':j.version,'performed_date':timezone.localdate(),'outcome':'complete','method':'grind','coordinates':'56.123456, 74.123456','note':''}
        f=ReviewCorrectionForm(data=data,job=j,user=self.leader);self.assertFalse(f.is_valid());self.assertIn('note',f.errors)
        f=ReviewCorrectionForm(data={**data,'note':'Правка'},job=j,user=self.leader);self.assertTrue(f.is_valid(),f.errors);self.assertEqual(f.cleaned_data['latitude'],Decimal('56.123456'))
    def test_leader_pending_row_does_not_offer_master_form(self):
        j,r=self.make_job();self.client.force_login(self.leader);response=self.client.get(reverse('repairs'))
        self.assertContains(response,'Подтвердить');self.assertNotContains(response,reverse('repair_report',args=[j.pk,'repaired']))
