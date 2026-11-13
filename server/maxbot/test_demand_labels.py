from unittest.mock import patch
from django.test import TestCase,override_settings
from projects import test_stock as fixture
from repairs.models import Report
from . import tests as base,demand_watch
from .labels import location
from .models import DemandWatch,Outbox
from .dialog import job_title

@override_settings(**base.CONFIG)
class DemandLabelTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    make_job=fixture.StockTests.make_job;bind=base.MaxTests.bind
    def setUp(self):
        self.binding=self.bind(501,601,user=self.leader);Outbox.objects.all().delete()
    def test_equal_location_names_are_shown_once_but_different_sections_remain(self):
        self.assertEqual(location('ОИ Тест — Омск','ОИ Тест - Омск'),'ОИ Тест — Омск')
        self.assertEqual(location('Объект','Другой участок'),'Объект · Другой участок')
        job=self.make_job();job.section.name=self.obj.name;job.section.save()
        self.assertEqual(job_title(job).count(self.obj.name),1)
    def test_format_migration_baselines_without_fake_change_and_grouped_dn_notice(self):
        watch=DemandWatch.objects.create(binding=self.binding,digest='old',version=1,format_version=1)
        rows=[{'job':str(n),'contract':'ТЕСТ','contract_id':'A','object':'ТЕСТ участок','object_id':'O','section':'ТЕСТ участок','section_id':'S','diameter':720,'method':'p1','length':2000,'wall':'8','quantity':n} for n in (1,2)]
        with patch('maxbot.demand_watch.snapshot',return_value=('a'*64,rows)):demand_watch.queue_changes()
        self.assertFalse(Outbox.objects.exists());watch.refresh_from_db();self.assertEqual(watch.format_version,2)
        with patch('maxbot.demand_watch.snapshot',return_value=('b'*64,rows)):demand_watch.queue_changes()
        text=Outbox.objects.get().body['text'];self.assertEqual(text.count('ТЕСТ участок'),1);self.assertIn('DN 700',text);self.assertIn('3 шт.',text)
    def test_rename_is_not_a_material_change_in_snapshot(self):
        job=self.make_job()
        records=[{'job':job,'couplings':[{'method':'p1','length':2000,'wall':8,'missing':1}]}]
        with patch('maxbot.demand_watch.all_demand',return_value=records):before=demand_watch.snapshot(self.leader)[0]
        self.obj.name='ТЕСТ переименовано';self.obj.save();job.section.project_object=self.obj
        with patch('maxbot.demand_watch.all_demand',return_value=records):after=demand_watch.snapshot(self.leader)[0]
        self.assertEqual(before,after)
