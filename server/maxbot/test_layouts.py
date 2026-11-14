from unittest.mock import patch
from django.test import TestCase,override_settings
from projects import test_stock as fixture
from repairs.models import PipeLayout,Job,Defect,JobDefect
from repairs.test_layouts import xlsx,ROWS
from repairs import layouts as service
from . import tests as base,worker
from .models import Outbox

@override_settings(**base.CONFIG)
class NativeLayoutTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    bind=base.MaxTests.bind;event=base.MaxTests.event
    def setUp(self):
        base.MaxTests.setUp(self);self.leader_binding=self.bind(501,601,user=self.leader);Outbox.objects.all().delete()
    def lead(self,text='',photos=None):return self.event(text,photos,sender=501,chat=601)
    def reply(self,e):return Outbox.objects.get(key=f'in:{e.pk}:reply')
    def prepare(self):
        self.lead(f'layout:upload:{self.section.pk}')
        with patch('maxbot.layouts.download_file',side_effect=lambda *a:xlsx()):event=self.lead(photos=[{'type':'file','name':'layout.xlsx','url':'https://synthetic.invalid/a'}])
        self.assertIn('Секций: 3',self.reply(event).body['text']);return PipeLayout.objects.get()
    def test_full_upload_preview_sample_apply_and_repeat_without_link(self):
        self.lead('/раскладка');row=self.prepare();self.assertEqual(row.state,'preview')
        e=self.lead(f'layout:sample:{row.pk}');self.assertIn('3000.000000',self.reply(e).body['text'])
        self.assertNotIn("'type': 'link'",str(self.reply(e).body))
        self.lead(f'layout:apply:{row.pk}');row.refresh_from_db();self.assertEqual(row.state,'active')
        e=self.lead(f'layout:apply:{row.pk}');self.assertIn('уже используется',self.reply(e).body['text']);self.assertEqual(PipeLayout.objects.filter(state='active').count(),1)
    def test_cancel_and_non_manager_error_are_native(self):
        e=self.event('/раскладка');self.assertIn('недоступно',self.reply(e).body['text']);self.assertTrue(worker.delivery_allowed(self.reply(e)))
        row=self.prepare();self.lead('/отмена');row.refresh_from_db();self.assertEqual(row.state,'cancelled')
    def test_readonly_distance_and_queue_revocation_on_layout_or_acl_change(self):
        row=self.prepare();self.lead(f'layout:apply:{row.pk}')
        self.job.defects.update(pipe_section='100',kilometre=100)
        job=Job.objects.create(contract=self.contract,section=self.section,created_by=self.leader,assigned_to=self.worker,method='grind')
        d=Defect.objects.create(contract=self.contract,section=self.section,kilometre=106,pipe_section='120',number='987');JobDefect.objects.create(job=job,defect=d)
        e=self.event(f'layout:job:{job.pk}:0');out=self.reply(e);self.assertIn('6000,000 м',out.body['text']);self.assertTrue(worker.delivery_allowed(out))
        d.pipe_section='555';d.save();self.assertFalse(worker.delivery_allowed(out))
    def test_archival_import_and_ordinary_drafts_cannot_share_file_upload(self):
        self.leader_binding.menu_scope={'historical_upload':{'contract':'unused'}};self.leader_binding.save(update_fields=['menu_scope'])
        e=self.lead(f'layout:upload:{self.section.pk}');self.assertIn('Сначала',self.reply(e).body['text'])
        self.assertFalse(PipeLayout.objects.exists())
    def test_current_version_history_is_readable_inside_bot(self):
        row=self.prepare();self.lead(f'layout:apply:{row.pk}')
        e=self.lead(f'layout:versions:{self.section.pk}:0');self.assertIn(f'layout:version:{row.pk}',str(self.reply(e).body))
        e=self.lead(f'layout:version:{row.pk}');self.assertIn('Действует',self.reply(e).body['text']);self.assertTrue(worker.delivery_allowed(self.reply(e)))
    def test_distance_command_closes_old_archive_search(self):
        self.event('archive:search:archive');self.event('/расстояния')
        self.binding.refresh_from_db();self.assertNotIn('archive_search',self.binding.menu_scope)

    def buttons(self,event):
        return [b for row in self.reply(event).body.get('attachments',[{'payload':{'buttons':[]}}])[0]['payload']['buttons'] for b in row]

    def add_job(self,km,number,section=None,contract=None,state='planned'):
        section=section or self.section;contract=contract or self.contract
        job=Job.objects.create(contract=contract,section=section,created_by=self.leader,assigned_to=self.worker,method='grind',state=state)
        d=Defect.objects.create(contract=contract,section=section,kilometre=km,number=number)
        JobDefect.objects.create(job=job,defect=d);return job

    def test_choose_section_then_readable_jobs_and_return_without_link(self):
        from projects.models import ProjectObject,ContractObject
        from repairs.models import Section
        area=ProjectObject.objects.create(legal_entity=self.company,name='ТЕСТ Другой участок')
        ContractObject.objects.create(contract=self.contract,project_object=area)
        section=Section.objects.create(project_object=area,rnu='ТЕСТ',pipeline='ОИ',name='ТЕСТ')
        other=self.add_job(200,'222',section=section)
        selected=self.add_job(137.288,'402812')
        event=self.lead('/расстояния');buttons=self.buttons(event)
        self.assertEqual(len([b for b in buttons if b['payload'].startswith('layout:jobs:')]),2)
        self.assertIn('Выберите участок',self.reply(event).body['text'])
        event=self.lead(f'layout:jobs:{self.section.pk}:0');buttons=self.buttons(event)
        choice=next(b for b in buttons if b['payload']==f'layout:job:{selected.pk}:0')
        self.assertTrue(choice['text'].startswith('км 137.288 · №402812'))
        self.assertIn('Шлифовка',choice['text']);self.assertNotIn('\n',choice['text'])
        self.assertNotIn(str(other.pk),str(buttons));self.assertIn('/расстояния',[b['payload'] for b in buttons])
        event=self.lead(choice['payload']);self.assertIn('402812',self.reply(event).body['text'])
        self.assertIn(f'layout:jobs:{self.section.pk}:0',[b['payload'] for b in self.buttons(event)])

    def test_kilometre_order_shared_job_once_and_paging_stays_in_section(self):
        jobs=[self.job]+[self.add_job(k,str(k)) for k in [8,2,7,3,6,4,5]]
        d=Defect.objects.create(contract=self.contract,section=self.section,kilometre=2.1,number='second')
        JobDefect.objects.create(job=jobs[2],defect=d)
        e=self.lead(f'layout:jobs:{self.section.pk}:0');buttons=self.buttons(e)
        first=[b for b in buttons if b['payload'].startswith('layout:job:')]
        self.assertEqual([b['text'].split(' · ')[0] for b in first],[f'км {k}.000' for k in range(1,7)])
        self.assertTrue(all(len(b['text'])<=100 and '\n' not in b['text'] for b in first))
        e=self.lead(next(b['payload'] for b in buttons if b['text']=='Далее'))
        last=[b for b in self.buttons(e) if b['payload'].startswith('layout:job:')]
        self.assertEqual([b['text'].split(' · ')[0] for b in last],['км 7.000','км 8.000'])
        self.assertEqual(len({b['payload'] for b in first+last}),8)

    def test_current_lists_exclude_archive_and_completed_and_recheck_before_delivery(self):
        from projects.models import Contract,ContractObject,ObjectAccess
        archive=Contract.objects.create(legal_entity=self.company,number='ТЕСТ Архив',subject='ТЕСТ',work_kinds=['repairs'])
        ContractObject.objects.create(contract=archive,project_object=self.obj)
        old=self.add_job(50,'old',contract=archive)
        archive.status='archived';archive.save(update_fields=['status'])
        done=[self.add_job(60,str(i),state=state) for i,state in enumerate(['confirmed','reported','unrepairable'])]
        e=self.lead(f'layout:jobs:{self.section.pk}:0')
        self.assertNotIn(str(old.pk),str(self.buttons(e)))
        for j in done:self.assertNotIn(str(j.pk),str(self.buttons(e)))
        e=self.event('/расстояния');out=self.reply(e);self.assertTrue(worker.delivery_allowed(out))
        work_event=self.event(f'layout:jobs:{self.section.pk}:0');work_out=self.reply(work_event)
        self.job.state='confirmed';self.job.save(update_fields=['state'])
        self.assertFalse(worker.delivery_allowed(out));self.assertFalse(worker.delivery_allowed(work_out))
        self.job.state='planned';self.job.save(update_fields=['state'])
        ObjectAccess.objects.filter(user=self.worker,project_object=self.obj).delete()
        self.assertFalse(worker.delivery_allowed(out))
        e=self.event(f'layout:jobs:{self.section.pk}:0')
        self.assertIn('недоступно',self.reply(e).body['text']);self.assertNotIn('ТЕСТ125',str(self.buttons(e)))

    def test_empty_work_list_does_not_keep_a_section_name_after_access_revocation(self):
        from projects.models import ObjectAccess
        self.job.state='confirmed';self.job.save(update_fields=['state'])
        e=self.event(f'layout:jobs:{self.section.pk}:0');out=self.reply(e)
        self.assertIn('Нет доступных текущих работ',out.body['text']);self.assertTrue(worker.delivery_allowed(out))
        ObjectAccess.objects.filter(user=self.worker,project_object=self.obj).delete()
        self.assertFalse(worker.delivery_allowed(out))

    def test_old_next_button_returns_to_section_picker(self):
        e=self.lead('layout:jobs:1')
        self.assertIn('Выберите участок',self.reply(e).body['text'])
        self.assertIn(f'layout:jobs:{self.section.pk}:0',[b['payload'] for b in self.buttons(e)])
