import uuid,io
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from .test_stock import StockTests
from .models import StockItem,StockImportReceipt,StockBalance
from . import stock,stock_planning as planning,stock_groups as groups,stock_exchange as exchange
from repairs.models import Job,Defect,JobDefect

class Stock619Tests(StockTests):
    def setup_groups(self):
        self.a=self.wh('Володя Барабинск');self.b=self.wh('Омск')
        self.section.diameter_mm=720;self.section.save()
        data=self.data(name='Муфта П2',kind='coupling',unit='pcs',method='p2',diameter_mm=720,length_mm=1500,wall_mm=8)
        planning.receive(self.leader,data,self.a,10,self.today,'Поступление',uuid.uuid4())
        planning.receive(self.leader,data,self.b,5,self.today,'Поступление',uuid.uuid4())
        self.jobs=[]
        for km,n in [(100,3),(102,2),(105,1)]:
            job=Job.objects.create(contract=self.contract,section=self.section,method='p2',wall_mm=8,length_m=Decimal('1.5')*n,composition='+'.join(['1.5']*n),created_by=self.leader)
            defect=Defect.objects.create(contract=self.contract,section=self.section,kilometre=km,number=str(km));JobDefect.objects.create(job=job,defect=defect);self.jobs.append(job)
    def group(self,wh):return next(g for g in groups.groups(self.leader) if g['warehouse'] and g['warehouse'].pk==wh.pk)
    def export_tables(self):
        self.client.force_login(self.leader)
        response=self.client.get(reverse('stock_inventory_export'));self.assertEqual(response.status_code,200)
        return exchange.parse(io.BytesIO(response.content))
    def write_tables(self,tables):return SimpleUploadedFile('stock.xlsx',exchange.book([(name,exchange.HEADERS[name],[r['values'] for r in rows]) for name,rows in tables.items()]))
    def test_groups_are_separate_per_warehouse_and_bulk_distribution(self):
        self.setup_groups();g=self.group(self.a)
        self.assertEqual(len(groups.groups(self.leader)),2)
        groups.distribute(self.leader,g['token'],{str(j.pk):n for j,n in zip(self.jobs,[3,2,1])})
        g=self.group(self.a);self.assertEqual((g['quantity'],g['reserved'],g['free']),(10,6,4))
        self.assertEqual(self.group(self.b)['quantity'],5)
        self.assertEqual([d['quantity'] for d in g['distribution']],[3,2,1])
        with self.assertRaises(ValidationError):groups.distribute(self.leader,g['token'],{str(self.jobs[0].pk):11})
        with self.assertRaises(ValidationError):groups.distribute(self.leader,g['token'],{str(self.jobs[0].pk):Decimal('1.5')})
    def test_roundtrip_allocation_receipt_correction_serial_and_retry(self):
        self.setup_groups();tables=self.export_tables();ga=self.group(self.a)
        for e in tables['Остатки']:
            if e['values'][0]==ga['id']:e['values'][16]='11'
        tables['Распределение']=[{'row':n+2,'values':[ga['id'],planning.job_label(j),str(q),self.a.name]} for n,(j,q) in enumerate(zip(self.jobs,[3,2,1]))]
        first=tables['Изделия'][0];first['values'][3]='ЗАВ-619'
        with self.assertRaises(ValidationError):
            # Stale source values must never silently pass.
            bad={**tables,'Остатки':[{'row':2,'values':['bad']*20}]};exchange.apply(self.leader,bad)
        before=StockItem.objects.count();changes=exchange.apply(self.leader,tables,dry=True)
        self.assertTrue(changes);self.assertEqual(StockItem.objects.count(),before);self.assertFalse(StockItem.objects.filter(reserved_job__isnull=False).exists())
        token=uuid.uuid4();exchange.apply(self.leader,tables,token);exchange.apply(self.leader,tables,token)
        self.assertEqual(StockImportReceipt.objects.filter(token=token).count(),1)
        self.assertEqual((self.group(self.a)['quantity'],self.group(self.a)['reserved'],self.group(self.a)['free']),(11,6,5))
        self.assertTrue(StockItem.objects.filter(serial='ЗАВ-619').exists())
        with self.assertRaises(ValidationError):exchange.apply(self.leader,tables,uuid.uuid4())
    def test_atomic_bad_allocation_rolls_back_other_warehouse_correction(self):
        self.setup_groups();tables=self.export_tables();gb=self.group(self.b)
        for row in tables['Остатки']:row['values'][16]=str(Decimal(row['values'][16])+1)
        tables['Распределение']=[{'row':2,'values':[gb['id'],'Неизвестная работа','1',self.b.name]}]
        with self.assertRaises(ValidationError):exchange.apply(self.leader,tables)
        self.assertEqual((self.group(self.a)['quantity'],self.group(self.b)['quantity']),(10,5))
    def test_materials_and_electrodes_split_by_warehouse(self):
        self.a=self.wh('А');self.b=self.wh('Б')
        for wh in [self.a,self.b]:
            planning.receive(self.leader,self.data(name='Электроды',kind='electrode',unit='kg',electrode_brand='УОНИ',electrode_diameter=Decimal('3.2')),wh,Decimal('12.5'),self.today,'Получено',uuid.uuid4())
            planning.receive(self.leader,self.data(name='Герметик',kind='sealant',unit='pcs'),wh,5,self.today,'Получено',uuid.uuid4())
        self.assertEqual(len(groups.groups(self.leader)),4)
        tables=self.export_tables();exchange.apply(self.leader,tables)
        self.assertEqual(len(groups.groups(self.leader)),4)
    def test_factory_number_is_not_assigned_to_an_entire_batch(self):
        wh=self.wh();item=self.item(name='Муфта',kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8)
        self.move(item,q=10,dst=wh);data=groups.values(item);data.update(coupling=None,serial='НЕ-НА-ПАРТИЮ')
        with self.assertRaises(ValidationError):stock.save_item(self.leader,item.pk,item.version,data)

    def test_legacy_batch_splits_automatically_on_bulk_allocation(self):
        self.a=self.wh();item=self.item(name='Муфта',kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8)
        self.move(item,q=10,dst=self.a);j=self.make_job();j.length_m=3;j.composition='1+1+1';j.save()
        g=self.group(self.a);groups.distribute(self.leader,g['token'],{str(j.pk):3})
        g=self.group(self.a);self.assertEqual((g['quantity'],g['reserved'],g['free']),(10,3,7))
    def test_upload_preview_and_apply_are_visible_and_idempotent(self):
        self.setup_groups();tables=self.export_tables();url=reverse('stock_inventory_import')
        response=self.client.post(url,{'file':self.write_tables(tables)});self.assertEqual(response.status_code,200,response.context['form'].errors)
        preview=response.context['preview'];self.assertTrue(preview)
        for _ in range(2):self.assertEqual(self.client.post(url,{'apply':'1','preview':preview}).status_code,302)
    def test_stale_group_after_stock_move(self):
        self.setup_groups();tables=self.export_tables();g=self.group(self.a);item=g['members'][0]['item']
        stock.move(self.leader,item.pk,'transfer',1,self.a.pk,self.b.pk,self.today,'Перемещение',uuid.uuid4())
        with self.assertRaises(ValidationError):exchange.apply(self.leader,tables)
    def test_new_rows_and_readonly_protection(self):
        self.setup_groups();tables=self.export_tables()
        tables['Остатки'].append({'row':5,'values':['НОВАЯ-1','',self.a.name,'Изоляция','Изоляция','м²','','','','','','','','','','','25','нет',str(self.today),'Сокур']})
        exchange.apply(self.leader,tables)
        self.assertTrue(StockBalance.objects.filter(item__kind='insulation',quantity=25).exists())

    def test_factory_numbers_for_old_batch_and_reservation_identity_preserved(self):
        self.a=self.wh();item=self.item(name='Муфта',kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8)
        self.move(item,q=10,dst=self.a);tables=self.export_tables();g=self.group(self.a)
        for n in range(3):tables['Изделия'].append({'row':n+4,'values':[g['id'],'','','ЗАВ-'+str(n),'',self.a.name,'1','','','']})
        exchange.apply(self.leader,tables)
        self.assertEqual(sum(StockBalance.objects.values_list('quantity',flat=True)),10)
        self.assertEqual(StockItem.objects.exclude(serial='').count(),3)
        self.assertEqual(self.group(self.a)['quantity'],10)

    def test_existing_factory_assignment_survives_new_distribution(self):
        self.setup_groups();g=self.group(self.a);groups.distribute(self.leader,g['token'],{str(self.jobs[0].pk):3})
        before=set(StockItem.objects.filter(reserved_job=self.jobs[0]).values_list('pk',flat=True))
        g=self.group(self.a);groups.distribute(self.leader,g['token'],{str(self.jobs[0].pk):3,str(self.jobs[1].pk):2})
        self.assertEqual(set(StockItem.objects.filter(reserved_job=self.jobs[0]).values_list('pk',flat=True)),before)

    def test_import_new_electrodes_with_fractional_diameter(self):
        self.setup_groups();tables=self.export_tables()
        tables['Остатки'].append({'row':9,'values':['НОВАЯ-2','',self.a.name,'Электроды УОНИ','Электроды','кг','','','','','УОНИ','3.2','','','','','12.5','нет',str(self.today),'База']})
        exchange.apply(self.leader,tables)
        group=next(g for g in groups.groups(self.leader) if g['item'].kind=='electrode')
        self.assertEqual(group['quantity'],Decimal('12.5'))

import os,subprocess,json
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
class Stock619Browser(StaticLiveServerTestCase):
    def test_grouped_distribution_and_xlsx_roundtrip(self):
        from .models import LegalEntity,COMPANY_TAX_ID
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'Тест'})
        StockTests.setUpTestData.__func__(type(self))
        self.leader.set_password('123456');self.leader.save()
        fixture=Stock619Tests();fixture.leader=self.leader;fixture.company=self.company;fixture.section=self.section;fixture.today=self.today;fixture.contract=self.contract
        fixture.setup_groups()
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/stock619-browser.cjs')],env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_STOCK_JOBS':json.dumps([str(j.pk) for j in fixture.jobs])},capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(StockItem.objects.filter(reserved_job__isnull=False).count(),6)

# Supplemental cases use the same isolated stock fixture.
class Stock619HistoryTests(Stock619Tests):
    def test_installed_pieces_leave_stock_but_remain_in_group_history_and_excel(self):
        self.setup_groups();g=self.group(self.a);job=self.jobs[0]
        groups.distribute(self.leader,g['token'],{str(job.pk):3})
        items=list(StockItem.objects.filter(reserved_job=job).select_related('coupling'))
        from repairs.services import report_work
        report_work(self.leader,job.pk,'result',{'token':uuid.uuid4(),'expected_version':job.version,'performed_date':self.today,'outcome':'complete','method':'p2','length_m':job.length_m,'composition':job.composition,'note':'Монтаж','problem':'','latitude':None,'longitude':None,'couplings':[i.coupling for i in items],'files':[]})
        g=self.group(self.a);self.assertEqual((g['quantity'],g['reserved'],g['free'],g['installed']),(7,0,7,3))
        tables=self.export_tables();exchange.apply(self.leader,tables)
        self.assertEqual((self.group(self.a)['quantity'],self.group(self.a)['installed']),(7,3))

    def test_duplicate_work_labels_are_disambiguated_in_excel(self):
        self.setup_groups();original=self.jobs[0]
        duplicate=Job.objects.create(contract=self.contract,section=self.section,method='p2',wall_mm=8,length_m=original.length_m,composition=original.composition,created_by=self.leader)
        for d in original.active_defects:JobDefect.objects.create(job=duplicate,defect=d)
        labels=exchange.work_choices(self.leader)
        first=next(label for j,label in labels if j.pk==original.pk);second=next(label for j,label in labels if j.pk==duplicate.pk)
        self.assertNotEqual(first,second);self.assertEqual(exchange.label_job(self.leader,first).pk,original.pk)

from django.test import TransactionTestCase
from django.db import connection,connections,close_old_connections
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
import threading
@skipUnless(connection.vendor=='postgresql','Требуется PostgreSQL')
class Stock619Concurrency(TransactionTestCase):
    def test_stale_group_cannot_override_concurrent_distribution(self):
        from .models import LegalEntity,COMPANY_TAX_ID
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'Тест'})
        StockTests.setUpTestData.__func__(type(self))
        f=Stock619Tests();f.leader=self.leader;f.company=self.company;f.section=self.section;f.today=self.today;f.contract=self.contract
        f.setup_groups();g=f.group(f.a);barrier=threading.Barrier(2)
        def run(n):
            close_old_connections()
            try:
                barrier.wait(timeout=10);groups.distribute(self.leader,g['token'],{str(f.jobs[n].pk):n+1});return 'saved'
            except ValidationError:return 'conflict'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(run,[0,1]))
        self.assertCountEqual(result,['saved','conflict']);self.assertEqual(f.group(f.a)['quantity'],10)
