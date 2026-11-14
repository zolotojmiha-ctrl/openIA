from unittest.mock import patch
from django.test import TestCase,override_settings
from projects import test_stock as fixture
from projects.models import ProjectObject,ContractObject,ObjectAccess,ContractAccess,StockMovement
from repairs.models import Section,Job,Defect,JobDefect,Report,Attachment
from repairs.test_progress import photo
from . import tests as base,worker
from .models import Outbox,Draft

@override_settings(**base.CONFIG)
class WorkNavigationTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    bind=base.MaxTests.bind;event=base.MaxTests.event
    def setUp(self):
        base.MaxTests.setUp(self);self.bind(301,401,self.leader);Outbox.objects.all().delete()
    def lead(self,text='',photos=None):return self.event(text,photos,sender=301,chat=401)
    def reply(self,e):return Outbox.objects.filter(key__startswith=f'in:{e.pk}:').first()
    def buttons(self,e):return [b for row in self.reply(e).body.get('attachments',[{'payload':{'buttons':[]}}])[0]['payload']['buttons'] for b in row]
    def add_job(self,km,number,section=None,state='planned'):
        section=section or self.section
        j=Job.objects.create(contract=self.contract,section=section,created_by=self.leader,assigned_to=self.worker,method='grind',state=state)
        d=Defect.objects.create(contract=self.contract,section=section,kilometre=km,number=number);JobDefect.objects.create(job=j,defect=d);return j
    def second_section(self):
        obj=ProjectObject.objects.create(legal_entity=self.company,name='ТЕСТ Второй участок')
        ContractObject.objects.create(contract=self.contract,project_object=obj)
        s=Section.objects.create(project_object=obj,rnu='Новосибирское РНУ',pipeline='ОИ',name=obj.name)
        self.add_job(900,'900',s);return s
    def test_ddk_starts_with_section_then_only_that_sections_jobs(self):
        other=self.second_section();self.add_job(20,'twenty');self.add_job(5,'five')
        e=self.lead('ddk:list:0');b=self.buttons(e)
        self.assertIn('Выберите участок',self.reply(e).body['text'])
        self.assertIn(f'worknav:jobs:ddk:{other.pk}:0',str(b));self.assertNotIn('ddk:select:',str(b))
        self.assertFalse(Draft.objects.exists())
        e=self.lead(f'worknav:jobs:ddk:{self.section.pk}:0')
        choices=[b for b in self.buttons(e) if b['payload'].startswith('ddk:select:')]
        self.assertEqual([b['text'].split(' · ')[0] for b in choices],['км 1.000','км 5.000','км 20.000'])
        self.assertNotIn('900',str(choices));self.assertFalse(Draft.objects.exists())
        self.lead(choices[0]['payload']);self.assertEqual(Draft.objects.get().job_id,self.job.pk)
    def test_all_pickers_keep_section_and_sort_shared_jobs_once_across_pages(self):
        self.second_section()
        extra=[self.add_job(k,str(k)) for k in [8,2,7,3,6,4,5]]
        d=Defect.objects.create(contract=self.contract,section=self.section,kilometre=2.1,number='common-second')
        JobDefect.objects.create(job=extra[1],defect=d)
        for purpose,prefix in [('works','worknav:job:'),('ddk','ddk:select:'),('photos','progress:select:')]:
            e=self.lead(f'worknav:jobs:{purpose}:{self.section.pk}:0');buttons=self.buttons(e)
            first=[b for b in buttons if b['payload'].startswith(prefix)]
            self.assertEqual([b['text'].split(' · ')[0] for b in first],[f'км {k}.000' for k in range(1,7)])
            e=self.lead(next(b['payload'] for b in buttons if b['text']=='Далее'))
            last=[b for b in self.buttons(e) if b['payload'].startswith(prefix)]
            self.assertEqual([b['text'].split(' · ')[0] for b in last],['км 7.000','км 8.000'])
            self.assertEqual(len({b['payload'] for b in first+last}),8)
            self.assertIn(f'worknav:sections:{purpose}:0',str(self.buttons(e)))
        self.assertFalse(Draft.objects.exists())
    def test_leader_work_card_is_native_and_does_not_mix_completed_rows(self):
        done=self.add_job(30,'done',state='confirmed')
        e=self.lead('leader:works:0');self.assertIn(f'worknav:jobs:works:{self.section.pk}:0',str(self.buttons(e)))
        e=self.lead(f'worknav:jobs:works:{self.section.pk}:0');self.assertNotIn(str(done.pk),str(self.buttons(e)))
        self.assertTrue(all(b['type']=='callback' for b in self.buttons(e)))
        e=self.lead(f'worknav:job:{self.job.pk}');self.assertIn('№ТЕСТ125',self.reply(e).body['text'])
        for prefix in ['progress:select:','ddk:select:','layout:job:']:self.assertIn(prefix,str(self.buttons(e)))
        self.assertTrue(all(b['type']=='callback' for b in self.buttons(e)))
    def test_photo_menu_explains_add_or_history_and_leader_can_save_in_bot(self):
        e=self.lead('leader:progress:0');self.assertIn('Добавить фотографии',str(self.buttons(e)));self.assertIn('Полученные фотоотчёты',str(self.buttons(e)))
        e=self.lead('worknav:sections:history:0');self.assertIn('пока нет',self.reply(e).body['text']);self.assertIn('/фото',str(self.buttons(e)))
        e=self.lead('/фото');self.assertIn(f'worknav:jobs:photos:{self.section.pk}:0',str(self.buttons(e)))
        self.lead(f'worknav:jobs:photos:{self.section.pk}:0');self.lead(f'progress:select:{self.job.pk}')
        draft=Draft.objects.get(action='progress');before=StockMovement.objects.count()
        with patch('maxbot.progress.download_photo',side_effect=lambda _:photo()):self.lead(photos=[{'type':'image','url':'https://synthetic.invalid/photo'}])
        self.lead(f'progress:save:{draft.pk}');self.lead(f'progress:save:{draft.pk}')
        report=Report.objects.get();self.assertEqual(report.author_id,self.leader.pk);self.assertEqual(report.context['progress'],{'stages':[]})
        self.assertEqual(Attachment.objects.count(),1);self.assertEqual(StockMovement.objects.count(),before)
        self.job.refresh_from_db();self.assertEqual(self.job.state,'progress')
        e=self.lead(f'worknav:history:{self.job.pk}:0');self.assertIn('Фото за',self.reply(e).body['text']);self.assertIn('1 шт.',self.reply(e).body['text'])
    def test_access_revocation_cancels_lists_and_empty_headers_and_foreign_callbacks(self):
        hidden=self.second_section()
        e=self.event(f'worknav:jobs:works:{hidden.pk}:0');self.assertIn('недоступен',self.reply(e).body['text'])
        self.assertNotIn(hidden.project_object.name,self.reply(e).body['text'])
        e=self.event('worknav:sections:ddk:0');out=self.reply(e);self.assertTrue(worker.delivery_allowed(out))
        ContractAccess.objects.filter(user=self.worker).delete();self.assertFalse(worker.delivery_allowed(out))
        ContractAccess.objects.create(user=self.worker,contract=self.contract,granted_by=self.leader)
        self.job.state='confirmed';self.job.save(update_fields=['state'])
        e=self.event(f'worknav:jobs:works:{self.section.pk}:0');out=self.reply(e);self.assertTrue(worker.delivery_allowed(out))
        ObjectAccess.objects.filter(user=self.worker,project_object=self.obj).delete();self.assertFalse(worker.delivery_allowed(out))
    def test_browsing_other_sections_preserves_an_unsaved_photo_draft(self):
        other=self.second_section();self.lead(f'progress:select:{self.job.pk}');draft=Draft.objects.get(action='progress')
        with patch('maxbot.progress.download_photo',side_effect=lambda _:photo()):self.lead(photos=[{'type':'image','url':'https://synthetic.invalid/photo'}])
        self.lead('worknav:sections:works:0');self.lead(f'worknav:jobs:works:{other.pk}:0')
        draft.refresh_from_db();self.assertEqual(draft.photos.count(),1);self.assertEqual(draft.state,'photos');self.assertEqual(draft.raw['note'],'');self.assertFalse(Report.objects.exists())

    def test_master_work_list_also_sorts_by_kilometre_and_keeps_native_result_actions(self):
        self.add_job(20,'twenty');self.add_job(5,'five')
        e=self.event(f'section:{self.section.pk}:0')
        rows=[b for b in self.buttons(e) if b['payload'].startswith('job:')]
        self.assertEqual([b['text'].split(' · ')[0] for b in rows],['км 1.000','км 5.000','км 20.000'])
        e=self.event(rows[0]['payload']);self.assertIn(f'begin:{self.job.pk}:repaired',str(self.buttons(e)))
