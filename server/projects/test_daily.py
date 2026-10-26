import uuid,io
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import ValidationError,PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from . import test_crews as fixtures
from .models import DailyWork,DailyWorkRevision,DailyWorkDecision,Contract
from . import daily,daily_views
from .crews import assign_crew_place
from repairs.excel import workbook

class DailyTests(TestCase):
    setUpTestData=classmethod(fixtures.CrewTests.setUpTestData.__func__)
    crew=fixtures.CrewTests.crew
    assignment=fixtures.CrewTests.assignment
    def setup(self):
        self.contract.work_kinds=['diagnostics','insulation','valves','construction'];self.contract.save()
        return self.assignment(self.crew())
    def data(self,crew,**extra):return dict(record=str(uuid.uuid4()),version='0',date=str(self.today),stage='grind_long',place='Секция 7',width='300',length='2',area='6',crew=str(crew.pk),**extra)
    def submit(self,data,actor=None,kind='diagnostics'):return daily.submit(actor or self.master,self.contract.pk,self.obj.pk,kind,data)
    def test_width_revisions_preserve_confirmed_quantities(self):
        a=self.setup();row=self.submit(self.data(a));old=row.current_revision
        daily.decide(self.leader,row.pk,row.version,'confirmed');row.refresh_from_db()
        data=self.data(a);data.update(record=str(row.pk),version=str(row.version),width='200',length='1')
        self.submit(data,self.leader);row.refresh_from_db()
        self.assertEqual(row.active_revision_id,old.pk);self.assertEqual(old.data['width'],'300')
        self.assertEqual(row.current_revision.data['width'],'200');self.assertEqual(row.revisions.count(),2)
        daily.decide(self.second_leader(),row.pk,row.version,'confirmed');row.refresh_from_db();self.assertEqual(row.active_revision.data['length'],'1')
    def second_leader(self):
        from accounts.models import User
        return User.objects.create_user('+70000000008','123456',leader_slot=2,full_name='Второй',must_change_password=False)
    def test_insulation_excludes_all_sandblasting(self):
        a=self.setup();data=self.data(a);data['stage']='blast_insulation'
        with self.assertRaises(ValidationError):self.submit(data,kind='insulation')
        self.assertFalse(DailyWork.objects.exists())
    def test_km_rounding_and_unrelated_scope(self):
        a=self.setup();data=self.data(a);data.update(km_from='123.4565',km_to='124.00049')
        row=self.submit(data);self.assertEqual(row.current_revision.data['km_from'],'123.457');self.assertEqual(row.current_revision.data['km_to'],'124.000')
        with self.assertRaises((PermissionDenied,ValidationError)):daily.submit(self.master,self.contract.pk,self.obj2.pk,'diagnostics',self.data(a))
    def test_no_crew_no_access_and_closed_contract(self):
        a=self.setup();data=self.data(a);data['crew']=''
        with self.assertRaises(ValidationError):self.submit(data)
        from accounts.models import User
        outsider=User.objects.create_user('+70000000009','123456',full_name='Чужой',must_change_password=False)
        with self.assertRaises(PermissionDenied):self.submit(self.data(a),outsider)
        Contract.objects.filter(pk=self.contract.pk).update(status='archived')
        with self.assertRaises(PermissionDenied):self.submit(self.data(a))
    def test_return_requires_reason_and_ordinary_user_cannot_confirm(self):
        a=self.setup();row=self.submit(self.data(a))
        with self.assertRaises(PermissionDenied):daily.decide(self.master,row.pk,1,'confirmed')
        with self.assertRaises(ValidationError):daily.decide(self.leader,row.pk,1,'revision')
        daily.decide(self.leader,row.pk,1,'revision','Уточнить объём');row.refresh_from_db();self.assertEqual(row.state,'revision')
        with self.assertRaises(ValidationError):daily.decide(self.leader,row.pk,1,'confirmed')
        self.assertEqual(DailyWorkDecision.objects.count(),1)
    def test_confirmed_report_cannot_be_overwritten_by_master(self):
        a=self.setup();row=self.submit(self.data(a));daily.decide(self.leader,row.pk,1,'confirmed');row.refresh_from_db()
        d=self.data(a);d.update(record=str(row.pk),version=str(row.version),width='200')
        with self.assertRaises(ValidationError):self.submit(d)
    def test_downtime_is_separate_and_requires_reason(self):
        a=self.setup();data=self.data(a);data.update(stage='downtime',length='',area='',width='',downtime='4')
        with self.assertRaises(ValidationError):self.submit(data)
        data['reason']='Нет готовности заказчика';row=self.submit(data);self.assertEqual(row.current_revision.data['downtime'],'4')
    def test_excel_preview_atomic_apply_and_replay(self):
        a=self.setup();data=self.data(a)
        row=['','0',*[data.get(k,'') for k in daily.FIELDS]]
        file=SimpleUploadedFile('daily.xlsx',workbook([row],headers=daily_views.HEADERS))
        self.client.force_login(self.master);url=reverse('daily_excel',args=[self.contract.pk,self.obj.pk,'diagnostics'])
        response=self.client.post(url,{'file':file});self.assertFalse(DailyWork.objects.exists());self.assertTrue(response.context['preview'],response.context['form'].errors)
        token=response.context['preview'];self.assertEqual(self.client.post(url,{'apply':'1','preview':token}).status_code,302)
        self.client.post(url,{'apply':'1','preview':token});self.assertEqual(DailyWork.objects.count(),1)
    def test_bulk_confirmation_and_return_history(self):
        a=self.setup();rows=[self.submit(self.data(a)) for _ in range(2)]
        self.client.force_login(self.leader);url=reverse('daily_works',args=[self.contract.pk,self.obj.pk,'diagnostics'])
        response=self.client.get(url);self.assertContains(response,'Подтвердить выбранные');snapshot=response.context['snapshot']
        self.client.post(url,{'snapshot':snapshot,'selected':[str(rows[0].pk)],'all':'0'})
        self.assertEqual(DailyWork.objects.filter(state='confirmed').count(),1)
        self.assertContains(self.client.get(reverse('daily_detail',args=[rows[0].pk])),'Ширина зачистки')
    def test_wrong_object_and_stale_edit_cannot_change_existing_row(self):
        a=self.setup();row=self.submit(self.data(a));data=self.data(a);data.update(record=str(row.pk),version='0')
        with self.assertRaises(ValidationError):self.submit(data)
        self.assertEqual(DailyWorkRevision.objects.count(),1)
    def test_unchanged_confirmed_export_does_not_create_new_revision(self):
        a=self.setup();row=self.submit(self.data(a));daily.decide(self.leader,row.pk,1,'confirmed');row.refresh_from_db()
        d={**row.current_revision.data,'record':str(row.pk),'version':str(row.version)}
        result=self.submit(d,self.leader);self.assertEqual(result.state,'confirmed');self.assertEqual(result.version,row.version);self.assertEqual(row.revisions.count(),1)
    def test_contract_edit_cannot_detach_object_with_daily_records(self):
        a=self.setup();self.submit(self.data(a));self.client.force_login(self.leader)
        response=self.client.post(reverse('contract_edit',args=[self.contract.pk]),{'expected_version':self.contract.version,'number':self.contract.number,'subject':self.contract.subject,'status':'planned','work_kinds':['diagnostics'],'linked_objects':[str(self.obj2.pk)]})
        self.assertEqual(response.status_code,200);self.assertTrue(self.contract.objects_linked.filter(pk=self.obj.pk).exists())
