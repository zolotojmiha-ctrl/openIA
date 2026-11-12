import io,uuid,zipfile
from datetime import datetime,time,timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from django.core.exceptions import PermissionDenied,ValidationError
from . import test_stock as fixture,demand_mail
from .models import DemandMailing,DemandMailRun,StockMovement
from postoffice.models import Mailbox,Letter
from postoffice.services import process_one,message_for
from .demand_mail_xlsx import workbook

class DemandMailTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    make_job=fixture.StockTests.make_job
    wh=fixture.StockTests.wh
    data=fixture.StockTests.data
    item=fixture.StockTests.item
    move=fixture.StockTests.move

    def setUp(self):
        self.section.diameter_mm=720;self.section.save()
        self.job=self.make_job()
        self.box=Mailbox.objects.create(address='noreply@example.org',state='active',created_by=self.leader)
        self.config=DemandMailing.objects.create(name='ТЕСТ заказчик',owner=self.leader,mailbox=self.box,recipients='customer@example.org, second@example.org')
        self.config.project_objects.add(self.obj)
        self.client.force_login(self.leader)

    def test_customer_snapshot_has_demand_even_when_stock_fully_covers_it(self):
        wh=self.wh()
        item=self.item(kind='coupling',unit='pcs',method='p2',diameter_mm=720,length_mm=1000,wall_mm=8)
        self.move(item,dst=wh,q=10)
        data=demand_mail.snapshot(self.config,self.leader)
        self.assertEqual(data['rows'][0]['quantity'],1)
        self.assertEqual(data['rows'][0]['dn'],'DN 700')
        self.assertFalse({'reserved','free','missing','stock','warehouse'} & set(data['rows'][0]))
        payload=workbook(data)
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            content=''.join(z.read(name).decode() for name in z.namelist() if name.endswith('.xml'))
            for forbidden in ['Обеспечено','Не хватает','На складе','Закреплено','Свободно']:self.assertNotIn(forbidden,content)
            self.assertEqual(len([name for name in z.namelist() if 'worksheets/sheet' in name]),1)
            self.assertIn('DN 700',content)
        from repairs.importer import read_book
        from django.core.files.uploadedfile import SimpleUploadedFile
        cells=read_book(SimpleUploadedFile('demand.xlsx',payload),sheet_name='Потребность')['cells']
        self.assertEqual(cells[(7,6)],'1')
        self.assertEqual(StockMovement.objects.count(),1)

    def test_manual_send_works_when_schedule_off_and_is_idempotent(self):
        token=uuid.uuid4();data=demand_mail.snapshot(self.config,self.leader)
        run=demand_mail.enqueue(self.leader,self.config.pk,token,data['digest'])
        self.assertEqual(run.deliveries.count(),2)
        self.assertEqual(demand_mail.enqueue(self.leader,self.config.pk,token,data['digest']).pk,run.pk)
        self.assertEqual(Letter.objects.count(),2)
        for letter in Letter.objects.all():
            self.assertEqual(len(letter.attachments.get().data)>0,True)
            self.assertIn('не отвечайте',letter.body.lower())
            self.assertTrue(letter.body.endswith('Письмо сформировано автоматически. Пожалуйста, не отвечайте на него. По вопросам изготовления и поставки свяжитесь с ответственным представителем ООО «Армада-Сибирь».\nwww.armada-sibir.ru\ninfo@armada-sibir.ru\n+7 (922) 114 20 00'))
            self.assertEqual(letter.subject,demand_mail.SUBJECT)
            self.assertNotIn(',',letter.to)

    def test_changed_demand_requires_new_preview(self):
        data=demand_mail.snapshot(self.config,self.leader)
        self.job.length_m='1.5';self.job.save()
        with self.assertRaises(ValidationError):
            demand_mail.enqueue(self.leader,self.config.pk,uuid.uuid4(),data['digest'])
        self.assertFalse(Letter.objects.exists())

    def test_view_access_and_preview_do_not_send(self):
        for name in ['demand_mail_list','demand_mail_new']:
            self.assertEqual(self.client.get(reverse(name)).status_code,200)
        response=self.client.get(reverse('demand_mail_preview',args=[self.config.pk]))
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Отправить потребность сейчас')
        self.assertFalse(Letter.objects.exists())
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse('demand_mail_preview',args=[self.config.pk])).status_code,403)

    def test_schedule_queues_once_per_week_and_skips_backlog(self):
        now=datetime(2026,11,16,10,tzinfo=ZoneInfo('Asia/Omsk'))
        self.config.enabled=True;self.config.weekday=0;self.config.send_time=time(9)
        self.config.not_before=now-timedelta(days=60);self.config.save()
        demand_mail.queue_due(now);demand_mail.queue_due(now)
        self.assertEqual(DemandMailRun.objects.count(),1)
        self.assertEqual(Letter.objects.count(),2)
        self.assertEqual(DemandMailRun.objects.get().week,now.date())
        self.config.enabled=False;self.config.save()
        demand_mail.queue_due(now+timedelta(days=7))
        self.assertEqual(DemandMailRun.objects.count(),1)

    def test_disabled_schedule_stops_queued_automatic_mail(self):
        run=demand_mail.enqueue(self.leader,self.config.pk,uuid.uuid4(),automatic=True,week=timezone.localdate())
        self.assertIsNone(run)
        self.config.enabled=True;self.config.save()
        demand_mail.enqueue(self.leader,self.config.pk,uuid.uuid4(),automatic=True,week=timezone.localdate())
        self.config.enabled=False;self.config.save()
        with patch('postoffice.transport.deliver') as deliver:
            process_one();self.assertFalse(deliver.called)
        self.assertEqual(Letter.objects.filter(state='failed').count(),1)

    def test_revoked_scope_stops_queued_mail(self):
        demand_mail.enqueue(self.leader,self.config.pk,uuid.uuid4())
        self.leader.is_active=False;self.leader.save()
        with patch('postoffice.transport.deliver') as deliver:
            process_one();self.assertFalse(deliver.called)

    def test_worker_records_smtp_acceptance_without_claiming_read_receipt(self):
        run=demand_mail.enqueue(self.leader,self.config.pk,uuid.uuid4())
        with patch('postoffice.transport.deliver',return_value={}),patch('postoffice.transport.save_sent'):
            self.assertTrue(process_one())
        self.assertEqual(Letter.objects.filter(state='sent').count(),1)
        response=self.client.get(reverse('demand_mail_history',args=[self.config.pk]))
        self.assertContains(response,'Принято почтовым сервером')

    def test_completed_jobs_are_not_emailed(self):
        self.job.state='reported';self.job.save()
        self.assertEqual(demand_mail.snapshot(self.config,self.leader)['rows'],[])

    def test_invalid_config_form_cannot_mix_sections_from_other_object(self):
        from .models import ProjectObject
        from repairs.models import Section
        from .demand_mail_views import MailingForm
        other=ProjectObject.objects.create(legal_entity=self.company,name='Чужой объект')
        section=Section.objects.create(project_object=other,name='Другой',pipeline='ТЕСТ',rnu='ТЕСТ')
        form=MailingForm({'name':'ТЕСТ','mailbox':str(self.box.pk),'recipients':'customer@example.org','project_objects':[str(self.obj.pk)],'sections':[str(section.pk)],'weekday':'0','send_time':'09:00','timezone':'Asia/Omsk','expected_version':'0'},user=self.leader)
        self.assertFalse(form.is_valid())
        self.assertIn('sections',form.errors)

    def test_settings_save_and_disable_preserve_version_and_scope(self):
        payload={'name':'ТЕСТ Обновлено','mailbox':str(self.box.pk),'recipients':'a@example.org\nb@example.org','project_objects':[str(self.obj.pk)],'weekday':'1','send_time':'10:30','timezone':'Asia/Omsk','expected_version':str(self.config.version),'enabled':'on'}
        response=self.client.post(reverse('demand_mail_edit',args=[self.config.pk]),payload)
        self.assertEqual(response.status_code,302)
        self.config.refresh_from_db()
        self.assertTrue(self.config.enabled);self.assertEqual(self.config.version,2)
        self.assertEqual(self.config.recipients,'a@example.org, b@example.org')
        payload.pop('enabled');payload['expected_version']='2'
        self.assertEqual(self.client.post(reverse('demand_mail_edit',args=[self.config.pk]),payload).status_code,302)
        self.config.refresh_from_db();self.assertFalse(self.config.enabled)
        self.assertEqual(self.config.version,3)

    def test_new_settings_are_disabled_by_default_and_owner_is_current_user(self):
        payload={'name':'ТЕСТ Новая','mailbox':str(self.box.pk),'recipients':'a@example.org','project_objects':[str(self.obj.pk)],'weekday':'0','send_time':'09:00','timezone':'Asia/Omsk','expected_version':'0'}
        response=self.client.post(reverse('demand_mail_new'),payload)
        self.assertEqual(response.status_code,302)
        config=DemandMailing.objects.get(name='ТЕСТ Новая')
        self.assertEqual(config.owner_id,self.leader.pk)
        self.assertFalse(config.enabled)

    def test_single_recipient_refusal_is_not_reported_as_sent(self):
        demand_mail.enqueue(self.leader,self.config.pk,uuid.uuid4())
        with patch('postoffice.transport.deliver',return_value={'customer@example.org':(550,b'no')}):
            process_one()
        self.assertEqual(Letter.objects.filter(state='failed').count(),1)

    def test_uncertain_smtp_result_is_not_retried(self):
        self.config.recipients='a@example.org';self.config.save()
        demand_mail.enqueue(self.leader,self.config.pk,uuid.uuid4())
        with patch('postoffice.transport.deliver',side_effect=OSError('network')):
            self.assertTrue(process_one())
        self.assertEqual(Letter.objects.get().state,'uncertain')
        with patch('postoffice.transport.deliver') as deliver:
            self.assertFalse(process_one());self.assertFalse(deliver.called)

    def test_unknown_diameter_is_not_sent_as_a_guessed_dn(self):
        self.section.diameter_mm=721;self.section.save()
        with self.assertRaises(ValidationError):demand_mail.snapshot(self.config,self.leader)
        self.assertFalse(Letter.objects.exists())

    def test_demand_sender_name_keeps_authenticated_mailbox_and_normal_letters_unchanged(self):
        from email import policy
        from email.parser import BytesParser
        demand_mail.enqueue(self.leader,self.config.pk,uuid.uuid4())
        letter=Letter.objects.first()
        parsed=BytesParser(policy=policy.default).parsebytes(message_for(letter).as_bytes())
        self.assertEqual(parsed['From'].addresses[0].display_name,'Армада-Сибирь — потребность в муфтах')
        self.assertEqual(parsed['From'].addresses[0].addr_spec,self.box.address)
        normal=Letter.objects.create(mailbox=self.box,author=self.leader,to='normal@example.org',subject='ТЕСТ',body='ТЕСТ')
        self.assertEqual(str(message_for(normal)['From']),self.box.address)


    def test_details_map_common_p1_to_all_defects_without_multiplying_quantity(self):
        from repairs.models import Defect,JobDefect
        self.job.method='p1';self.job.length_m=4;self.job.composition='2+2';self.job.save()
        first=self.job.defects.get();first.kilometre='100.100';first.number='TEST-A';first.pipe_section='PIPE-A';first.save()
        other=Defect.objects.create(contract=self.contract,section=self.section,kilometre='101.250',number='TEST-B',pipe_section='PIPE-B')
        JobDefect.objects.create(job=self.job,defect=other)
        data=demand_mail.snapshot(self.config,self.leader)
        self.assertEqual(len(data['details']),1)
        line=data['details'][0]
        self.assertEqual((line['kilometres'],line['defect_numbers'],line['length'],line['quantity']),('100.100; 101.250','TEST-A; TEST-B',2000,2))
        self.assertEqual(data['rows'][0]['quantity'],2)
        from repairs.importer import read_book
        cells=read_book(SimpleUploadedFile('demand.xlsx',workbook(data)),sheet_name='Потребность')['cells']
        detail_row=next(r for (r,c),v in cells.items() if c==2 and v==line['kilometres'])
        self.assertEqual(cells[(detail_row,3)],'TEST-A; TEST-B')
        self.assertEqual(cells[(detail_row,9)],'2')
        self.assertFalse({'missing','reserved','free','warehouse'} & set(line))
        for route in ('stock_coverage','repair_materials'):
            response=self.client.get(reverse(route))
            groups=response.context['rows' if route=='stock_coverage' else 'grouped']
            group=next(r for r in groups if r['need']==2)
            self.assertEqual(sum(p['quantity'] for p in group['placements']),2)
            self.assertContains(response,'Показать по километрам')
            self.assertContains(response,'TEST-A; TEST-B')

    def test_details_include_covered_work_and_km_change_invalidates_preview(self):
        wh=self.wh();item=self.item(kind='coupling',unit='pcs',method='p2',diameter_mm=720,length_mm=1000,wall_mm=8)
        self.move(item,dst=wh,q=10)
        data=demand_mail.snapshot(self.config,self.leader)
        self.assertEqual(data['details'][0]['quantity'],1)
        for route in ('stock_coverage','repair_materials'):
            response=self.client.get(reverse(route))
            group=next(r for r in response.context['rows' if route=='stock_coverage' else 'grouped'] if r['need']==1)
            self.assertEqual(group['missing'],0)
            self.assertEqual(len(group['placements']),1)
        d=self.job.defects.get();d.kilometre='2.345';d.save()
        with self.assertRaises(ValidationError):demand_mail.enqueue(self.leader,self.config.pk,uuid.uuid4(),data['digest'])
        self.assertFalse(Letter.objects.exists())

    def test_sent_snapshot_attachment_remains_original_when_locations_change(self):
        run=demand_mail.enqueue(self.leader,self.config.pk,uuid.uuid4())
        before=bytes(run.deliveries.first().letter.attachments.get().data)
        d=self.job.defects.get();d.kilometre='2.345';d.save()
        run.refresh_from_db()
        self.assertEqual(run.snapshot['details'][0]['kilometres'],'1.000')
        self.assertEqual(bytes(run.deliveries.first().letter.attachments.get().data),before)

    def test_details_exclude_completed_jobs_and_keep_scope(self):
        from repairs.models import Section,Job
        from .models import ProjectObject
        other=ProjectObject.objects.create(legal_entity=self.company,name='ТЕСТ вне рассылки')
        section=Section.objects.create(project_object=other,name='ТЕСТ',pipeline='ТЕСТ',rnu='ТЕСТ',diameter_mm=720)
        Job.objects.create(section=section,method='p2',length_m=1,wall_mm=8,created_by=self.leader)
        for state in ('reported','confirmed','unrepairable'):
            self.job.state=state;self.job.save()
            self.assertEqual(demand_mail.snapshot(self.config,self.leader)['details'],[])


    def test_partial_p1_exports_only_remaining_construction(self):
        from repairs.models import Coupling,CouplingUse,Report
        self.job.method='p1';self.job.length_m='2.5';self.job.composition='1+1.5';self.job.state='partial';self.job.save()
        report=Report.objects.create(job=self.job,author=self.leader,kind='partial',performed_date=self.today)
        installed=Coupling.objects.create(project_object=self.obj,method='p1',length_mm=1000,wall_mm=8,serial='TEST-INSTALLED')
        CouplingUse.objects.create(job=self.job,coupling=installed,report=report)
        data=demand_mail.snapshot(self.config,self.leader)
        self.assertEqual([(r['length'],r['quantity']) for r in data['details']],[(1500,1)])
        self.assertEqual(data['details'][0]['state'],self.job.get_state_display())
        self.assertNotIn('TEST-INSTALLED',str(data))
