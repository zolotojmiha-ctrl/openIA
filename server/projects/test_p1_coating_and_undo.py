import uuid
from decimal import Decimal
from unittest.mock import patch
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from accounts.models import AccessGrant
from repairs.importer import read_book
from repairs.models import Coupling, Report
from repairs.services import report_work
from . import test_stock as base, repair_materials as materials, stock, stock_planning
from .models import StockMovement, RepairConsumption
from .p1_coating import TABLE, row


class P1CoatingAndUndoTests(TestCase):
    setUpTestData=classmethod(base.StockTests.setUpTestData.__func__)
    wh=base.StockTests.wh
    data=base.StockTests.data
    item=base.StockTests.item
    move=base.StockTests.move
    balance=base.StockTests.balance
    make_job=base.StockTests.make_job

    def setUp(self):
        self.section.diameter_mm=720;self.section.save()

    def job(self,length='4',composition='2+2'):
        job=self.make_job();job.method='p1';job.length_m=Decimal(length);job.composition=composition;job.save();return job

    def supplies(self,skip=()):
        warehouse=self.wh(name='ТЕСТ Материалы П1',project_object=self.obj);items={}
        for kind in ['sealant','composite','primer','ksizol','wrapper']:
            if kind in skip:continue
            item=self.item(sku='P1-'+kind,name='ТЕСТ '+kind,kind=kind,unit='kit' if kind in ['sealant','composite'] else 'kg')
            self.move(item,dst=warehouse,q=100);items[kind]=item
        return warehouse,items

    def pieces(self,warehouse,lengths=(2000,2000)):
        pieces=[]
        for index,length in enumerate(lengths):
            piece=Coupling.objects.create(project_object=self.obj,method='p1',length_mm=length,wall_mm=8,serial='ТЕСТ П1 '+str(index))
            item=self.item(sku='PIECE-'+str(index),kind='coupling',unit='pcs',method='p1',length_mm=length,wall_mm=8,diameter_mm=720,coupling=piece)
            self.move(item,dst=warehouse,q=1);pieces.append(piece)
        return pieces

    def send(self,job,pieces,action='result',outcome='complete',token=None):
        job.refresh_from_db();length=sum(p.length_mm for p in pieces)/1000
        return report_work(self.worker if action=='result' else self.leader,job.pk,action,{
            'token':token or uuid.uuid4(),'expected_version':job.version,'performed_date':self.today,
            'outcome':outcome,'method':'p1','length_m':Decimal(str(length)),
            'composition':'+'.join(str(Decimal(p.length_mm)/1000) for p in pieces),
            'note':'ТЕСТ П1','problem':'','latitude':Decimal(55),'longitude':Decimal(73),'couplings':pieces,'files':[]})

    def test_all_approved_rounded_mass_rows_are_used_literally(self):
        expected=[('1.1','26.3','4.1'),('1.3','32.9','5.1'),('1.6','39.5','6.2'),('1.9','46.1','7.2'),('2.1','52.7','8.2'),('2.4','59.2','9.2'),('2.7','65.8','10.3'),('2.9','72.4','11.3'),('3.2','79.0','12.3'),('3.5','85.6','13.4'),('3.7','92.1','14.4'),('4.0','98.7','15.4'),('4.3','105.3','16.4'),('4.5','111.9','17.5'),('4.8','118.5','18.5')]
        self.assertEqual(len(TABLE['rows']),15)
        for index,quantities in enumerate(expected):
            value=row(720,1000+500*index)
            self.assertEqual(tuple(value[k] for k in ['primer_kg','ksizol_kg','wrapper_kg']),quantities)
        value=row(720,1500)
        self.assertEqual((value['cleaning_length_m'],value['cleaning_area_m2'],value['area_m2']),('1.90','4.30','6.34'))

    def test_composite_is_one_full_length_not_sum_of_individual_coatings(self):
        job=self.job();pieces=[type('Piece',(),{'length_mm':2000})() for _ in range(2)]
        lines=materials.norm(job,pieces)
        values={line['kind']:Decimal(line['quantity']) for line in lines}
        self.assertEqual(values['primer'],Decimal('2.7'));self.assertEqual(values['ksizol'],Decimal('65.8'));self.assertEqual(values['wrapper'],Decimal('10.3'))
        self.assertEqual(len(lines),5)
        self.assertEqual({line['quantity'] for line in lines if line['kind']=='composite'},{str(materials.kits(720,4000)['composite'])})

    def test_actual_larger_piece_uses_its_approved_row_without_rewriting_requirement(self):
        job=self.job('1','');piece=type('Piece',(),{'length_mm':1500})()
        values={line['kind']:line['quantity'] for line in materials.norm(job,[piece])}
        self.assertEqual(values['primer'],'1.3');self.assertEqual(values['ksizol'],'32.9')
        job.refresh_from_db();self.assertEqual(job.length_m,1)

    def test_other_sizes_are_not_extrapolated_and_known_kits_remain_in_demand(self):
        for diameter,length in [(1020,1000),(720,8500),(720,1250)]:
            with self.assertRaises(ValidationError):row(diameter,length)
        job=self.job('8.5','');record=next(r for r in materials.all_demand() if r['job']==job)
        self.assertEqual({line['kind'] for line in record['materials']},{'sealant','composite'})
        self.assertTrue(any('В таблице ПТО нет' in issue for issue in record['issues']))

    def test_p1_norms_are_visible_in_demand_and_numeric_excel(self):
        job=self.job();self.client.force_login(self.leader)
        response=self.client.get(reverse('repair_materials_export'))
        cells=read_book(SimpleUploadedFile('needs.xlsx',response.content),sheet_name='Потребность')['cells']
        record=next(r for r in range(2,20) if cells.get((r,1))=='Ксизол')
        self.assertEqual(Decimal(cells[(record,3)]),Decimal('65.8'))
        self.assertContains(self.client.get(reverse('coating_profiles')),'муфта 760')

    def test_complete_p1_posts_all_five_materials_once_and_keeps_norm_snapshot(self):
        job=self.job();warehouse,items=self.supplies();pieces=self.pieces(warehouse);token=uuid.uuid4()
        self.send(job,pieces,token=token);entry=RepairConsumption.objects.get(job=job)
        self.assertEqual(entry.state,'posted');self.assertEqual(len(entry.lines),5)
        self.assertEqual(self.balance(warehouse,items['wrapper']),Decimal('89.7'))
        self.assertEqual(self.balance(warehouse,items['ksizol']),Decimal('34.2'))
        self.assertEqual(self.balance(warehouse,items['sealant']),100-materials.kits(720,4000)['sealant'])
        before=StockMovement.objects.count();self.send(job,pieces,action='correction')
        self.assertEqual(StockMovement.objects.count(),before)
        for line in entry.lines:
            self.assertTrue(line['movements']);self.assertTrue(line['allocations'])

    def test_shortage_never_partially_consumes_materials_and_retry_posts_once(self):
        job=self.job();warehouse,items=self.supplies(skip=['wrapper']);pieces=self.pieces(warehouse)
        self.send(job,pieces);entry=RepairConsumption.objects.get(job=job);self.assertEqual(entry.state,'pending')
        for item in items.values():self.assertEqual(self.balance(warehouse,item),100)
        wrapper=self.item(sku='P1-wrapper',kind='wrapper',unit='kg');self.move(wrapper,dst=warehouse,q=100)
        materials.retry(self.leader,job);entry.refresh_from_db();self.assertEqual(entry.state,'posted')
        self.assertEqual(self.balance(warehouse,wrapper),Decimal('89.7'))
        with self.assertRaises(ValidationError):materials.retry(self.leader,job)

    def test_old_completed_p1_is_not_silently_backfilled_by_correction(self):
        job=self.job();warehouse,items=self.supplies();pieces=self.pieces(warehouse)
        with patch('projects.repair_materials.reconcile',return_value=None):self.send(job,pieces)
        self.assertFalse(RepairConsumption.objects.filter(job=job).exists())
        before=StockMovement.objects.count();self.send(job,pieces,action='correction')
        self.assertEqual(RepairConsumption.objects.get(job=job).state,'review')
        self.assertEqual(StockMovement.objects.count(),before)
        for item in items.values():self.assertEqual(self.balance(warehouse,item),100)

    def test_existing_manual_consumption_is_preserved_without_double_deduction(self):
        job=self.job();warehouse,items=self.supplies();pieces=self.pieces(warehouse)
        manual=self.move(items['primer'],'consume',Decimal('2.7'),src=warehouse,job=job)
        self.send(job,pieces)
        self.assertEqual(RepairConsumption.objects.get(job=job).state,'review')
        self.assertEqual(self.balance(warehouse,items['primer']),Decimal('97.3'))
        self.assertEqual(self.balance(warehouse,items['ksizol']),100)
        self.assertEqual(RepairConsumption.objects.get(job=job).review_reason,'manual')
        stock.reverse(self.leader,manual.pk,'ТЕСТ Ошибочная ручная операция',uuid.uuid4())
        materials.retry(self.leader,job)
        self.assertEqual(RepairConsumption.objects.get(job=job).state,'posted')
        self.assertEqual(self.balance(warehouse,items['primer']),Decimal('97.3'))
        self.assertEqual(self.balance(warehouse,items['ksizol']),Decimal('34.2'))

    def test_manual_spending_while_pending_is_not_duplicated_on_retry(self):
        job=self.job();warehouse,items=self.supplies(skip=['wrapper']);pieces=self.pieces(warehouse)
        self.send(job,pieces)
        self.move(items['primer'],'consume',Decimal('2.7'),src=warehouse,job=job)
        wrapper=self.item(sku='P1-wrapper',kind='wrapper',unit='kg');self.move(wrapper,dst=warehouse,q=100)
        materials.retry(self.leader,job)
        self.assertEqual(RepairConsumption.objects.get(job=job).state,'review')
        self.assertEqual(self.balance(warehouse,items['primer']),Decimal('97.3'))
        self.assertEqual(self.balance(warehouse,wrapper),100)

    def deleted_kits(self):
        warehouse=self.wh();item=self.item(kind='sealant',unit='kit',name='ТЕСТ Герметик 24')
        self.move(item,dst=warehouse,q=24);stock_planning.delete(self.leader,item.pk,item.version,'1')
        return warehouse,item,StockMovement.objects.get(item=item,kind='loss')

    def test_deleted_24_kits_can_be_restored_with_card_and_full_history(self):
        warehouse,item,loss=self.deleted_kits();item.refresh_from_db();self.assertTrue(item.archived);self.assertIsNotNone(item.deleted_at)
        token=uuid.uuid4();reversal=stock.reverse(self.leader,loss.pk,'ТЕСТ Ошибочно удалили',token)
        item.refresh_from_db();self.assertFalse(item.archived);self.assertIsNone(item.deleted_at)
        self.assertEqual(self.balance(warehouse,item),24);self.assertEqual(reversal.reverses_id,loss.pk)
        self.assertEqual(item.movements.count(),3)
        self.assertEqual(stock.reverse(self.leader,loss.pk,'ТЕСТ Ошибочно удалили',token).pk,reversal.pk)
        with self.assertRaises(ValidationError):stock.reverse(self.leader,loss.pk,'Повтор',uuid.uuid4())
        self.assertEqual(self.balance(warehouse,item),24)

    def test_cannot_restore_onto_archived_warehouse_and_card_change_rolls_back(self):
        warehouse,item,loss=self.deleted_kits();warehouse.archived=True;warehouse.save()
        with self.assertRaises(ValidationError):stock.reverse(self.leader,loss.pk,'ТЕСТ Отмена',uuid.uuid4())
        item.refresh_from_db();self.assertTrue(item.archived);self.assertIsNotNone(item.deleted_at)
        self.assertEqual(self.balance(warehouse,item),0);self.assertFalse(StockMovement.objects.filter(reverses=loss).exists())

    def test_cancel_warehouse_deletion_keeps_other_warehouse_quantity(self):
        first=self.wh('ТЕСТ Первый');second=self.wh('ТЕСТ Второй');item=self.item(kind='sealant',unit='kit')
        self.move(item,dst=first,q=24);self.move(item,dst=second,q=3)
        stock_planning.delete(self.leader,item.pk,item.version,'ТЕСТ',warehouse_id=first.pk)
        loss=StockMovement.objects.get(item=item,kind='loss');stock.reverse(self.leader,loss.pk,'ТЕСТ Отмена',uuid.uuid4())
        self.assertEqual(self.balance(first,item),24);self.assertEqual(self.balance(second,item),3)

    def test_partial_p1_gets_one_full_construction_consumption_only_on_completion(self):
        job=self.job('2.5','1+1.5');warehouse,items=self.supplies();pieces=self.pieces(warehouse,(1000,1500))
        self.send(job,pieces[:1],outcome='partial');self.assertFalse(RepairConsumption.objects.filter(job=job).exists())
        from .material_views import job_panel
        job.refresh_from_db();self.assertEqual(job_panel(self.leader,job)['coating_geometry']['length_mm'],2500)
        self.send(job,pieces);entry=RepairConsumption.objects.get(job=job)
        self.assertEqual(entry.state,'posted');self.assertEqual(self.balance(warehouse,items['ksizol']),Decimal('53.9'))

    def test_undo_ui_names_deletion_correctly_and_hides_cancel_after_reversal(self):
        warehouse,item,loss=self.deleted_kits();self.client.force_login(self.leader)
        response=self.client.get(reverse('stock_detail',args=[warehouse.pk]))
        self.assertContains(response,'Восстановить удалённое');self.assertNotContains(response,'· По результату ремонта')
        url=reverse('stock_reverse',args=[loss.pk]);self.assertContains(self.client.get(url),'снова появится в наличии')
        self.assertEqual(self.client.post(url,{'note':'ТЕСТ Ошибка ввода','token':uuid.uuid4()}).status_code,302)
        response=self.client.get(reverse('stock_detail',args=[warehouse.pk]));self.assertContains(response,'Отменено обратной записью')
        self.assertNotContains(response,'Восстановить удалённое')

    def test_undo_requires_stock_permission_and_cannot_cancel_repair_materials_alone(self):
        warehouse,item,loss=self.deleted_kits()
        with self.assertRaises(PermissionDenied):stock.reverse(self.worker,loss.pk,'ТЕСТ',uuid.uuid4())
        job=self.job();wh,items=self.supplies();pieces=self.pieces(wh);self.send(job,pieces)
        consumption=StockMovement.objects.filter(job=job,kind='consume',item__kind='wrapper').get()
        with self.assertRaises(ValidationError):stock.reverse(self.leader,consumption.pk,'ТЕСТ',uuid.uuid4())
