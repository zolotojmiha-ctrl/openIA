import uuid
from django.test import TestCase,override_settings
from projects import test_stock as fixtures,stock_planning
from projects.models import StockItem,StockMovement,StockBalance
from repairs.installation_stock import source_key
from repairs.models import Report
from . import tests as base,dialog
from .models import Outbox


@override_settings(**base.CONFIG)
class NativeInstallationStockTests(TestCase):
    setUpTestData=classmethod(fixtures.StockTests.setUpTestData.__func__)
    setUp=base.MaxTests.setUp;bind=base.MaxTests.bind;event=base.MaxTests.event
    draft=base.MaxTests.draft;fill=base.MaxTests.fill
    wh=fixtures.StockTests.wh;data=fixtures.StockTests.data;make_job=fixtures.StockTests.make_job

    def prepare(self,count=1,reserve=False):
        self.job.method='p1';self.job.length_m=2;self.job.wall_mm=10;self.job.composition='2';self.job.save()
        warehouse=self.wh(project_object=self.obj)
        stock_planning.receive(self.leader,self.data(kind='coupling',unit='pcs',method='p1',diameter_mm=self.section.diameter_mm,length_mm=2000,wall_mm=10),warehouse,count,self.today,'ТЕСТ',uuid.uuid4())
        items=list(StockItem.objects.all())
        if reserve:
            for item in items:
                donor=self.make_job();donor.method='p1';donor.length_m=2;donor.wall_mm=10;donor.composition='2';donor.save()
                stock_planning.reserve(self.leader,item.pk,item.version,donor.pk)
        return items

    def test_native_wall_and_free_inventory_consumption(self):
        item=self.prepare()[0]
        draft=self.draft()
        self.assertIn('wall_mm',draft.fields)
        draft=self.fill(draft,{'wall_mm':'10','length_m':'2','composition':'2','manual_serials':'ТЕСТ НОВЫЙ НОМЕР'})
        self.event('review:'+str(draft.pk));self.event('confirm:'+str(draft.pk))
        self.assertEqual(StockMovement.objects.filter(kind='consume').count(),1)
        item.refresh_from_db();self.assertEqual(item.serial,'ТЕСТ НОВЫЙ НОМЕР')

    def test_native_explicit_donor_choice_is_saved_with_report(self):
        item=self.prepare(reserve=True)[0]
        donor=StockItem.objects.get(pk=item.pk).reserved_job_id
        draft=self.fill(self.draft(),{'wall_mm':'10','length_m':'2','composition':'2',
            'manual_serials':'ТЕСТ ПЕРЕНОС','stock_source_0':source_key(item.pk,StockBalance.objects.get(item=item).warehouse_id)})
        self.event('review:'+str(draft.pk));self.event('confirm:'+str(draft.pk))
        report=Report.objects.get()
        self.assertEqual(report.context['stock_bindings'][0]['previous_job'],str(donor))
        self.assertEqual(StockMovement.objects.filter(kind='consume').count(),1)

    def test_source_buttons_have_next_page_without_losing_later_reservations(self):
        self.prepare(count=10,reserve=True)
        draft=self.draft()
        # Advance through ordinary facts; stop at the source question.
        for _ in range(30):
            draft.refresh_from_db()
            name=draft.fields[draft.step]
            if name=='stock_source_0':break
            form=dialog.form_for(draft,self.worker)
            value={'coordinates':'55, 73','wall_mm':'10','length_m':'2','composition':'2',
                   'manual_serials':'ТЕСТ СТРАНИЦА','actual_coating_length_m':'2.4',
                   'performed_date':str(self.today)}.get(name,'')
            self.event(value)
        row=Outbox.objects.order_by('-created_at').first()
        self.assertIn('Следующие источники',str(row.body))
        self.event(f'sourcepage:{draft.pk}:{draft.step}:1')
        row=Outbox.objects.order_by('-created_at').first()
        self.assertIn('Предыдущие источники',str(row.body))
        self.assertNotIn('Следующие источники',str(row.body))
        self.assertFalse(StockMovement.objects.filter(kind='consume').exists())

    def test_native_actual_override_does_not_force_reserved_dimensions(self):
        free=self.prepare()[0]
        warehouse=free.balances.get().warehouse
        stock_planning.receive(self.leader,self.data(kind='coupling',unit='pcs',method='p1',diameter_mm=self.section.diameter_mm,length_mm=2000,wall_mm=12),warehouse,1,self.today,'ТЕСТ',uuid.uuid4())
        reserved=StockItem.objects.get(wall_mm=12)
        stock_planning.reserve(self.leader,reserved.pk,reserved.version,self.job.pk)
        draft=self.fill(self.draft(),{'installation_override':'yes','wall_mm':'10','length_m':'2','composition':'2','manual_serials':'ТЕСТ ДРУГОЙ СОСТАВ'})
        self.event('review:'+str(draft.pk));self.event('confirm:'+str(draft.pk))
        reserved.refresh_from_db();free.refresh_from_db()
        self.assertIsNone(reserved.reserved_job_id)
        self.assertEqual(free.serial,'ТЕСТ ДРУГОЙ СОСТАВ')
        self.assertEqual(StockMovement.objects.get(kind='consume').item_id,free.pk)
