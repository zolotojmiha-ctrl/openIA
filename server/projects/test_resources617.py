import uuid,subprocess,os,json
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from .test_workflow6161 import WorkflowTests
from .models import Equipment,EquipmentAssignment,ProjectObject,ContractObject,Warehouse,StockItem,StockBalance,StockMovement,Brigade
from documents.models import Person
from . import stock

class ResourceTests(WorkflowTests):
    def setUp(self):
        super().setUp()
        self.today=timezone.localdate()
        self.second=ProjectObject.objects.create(legal_entity=self.entity,name='Барабинск–Татарская',status='active')
        ContractObject.objects.create(contract=self.contract,project_object=self.second)
        person=Person.objects.create(legal_entity=self.entity,full_name='Зарипов',user=self.master,created_by=self.leader)
        self.crew=Brigade.objects.create(legal_entity=self.entity,name='Бригада Зарипова',foreman=person,created_by=self.leader)
        self.crew2=Brigade.objects.create(legal_entity=self.entity,name='Другая бригада',foreman=person,created_by=self.leader)
        self.cars=[Equipment.objects.create(legal_entity=self.entity,name='Соболь',category='Авто',inventory_number=str(i),registration_number='Р'+str(i),created_by=self.leader) for i in [721,992]]
    def picker_data(self):
        url=reverse('area_equipment_pick',args=[self.contract.pk,self.area.pk])
        token=self.client.get(url).context['form'].initial['snapshot']
        return url,{'snapshot':token,'equipment':[str(e.pk) for e in self.cars],'start_date':str(self.today)}
    def test_two_cars_one_submit_context_and_atomic_conflict(self):
        url,data=self.picker_data()
        self.cars[1].version+=1;self.cars[1].save(update_fields=['version'])
        response=self.client.post(url,data)
        self.assertContains(response,'Техника изменена')
        self.assertFalse(EquipmentAssignment.objects.exists())
        url,data=self.picker_data()
        response=self.client.post(url,data)
        self.assertEqual(response.status_code,302)
        self.assertEqual(EquipmentAssignment.objects.filter(project_object=self.area,contract=self.contract).count(),2)
        self.assertTrue(response.url.endswith('#object-equipment'))
        response=self.client.get(reverse('object_detail',args=[self.area.pk]),{'contract':self.contract.pk})
        self.assertContains(response,'721');self.assertContains(response,'992')
        self.assertContains(response,'Вся техника компании')
    def test_picker_forbidden_and_wrong_contract(self):
        url,data=self.picker_data();self.client.force_login(self.master)
        self.assertEqual(self.client.post(url,data).status_code,403)
        self.assertFalse(EquipmentAssignment.objects.exists())
    def test_shared_warehouse_two_areas_two_crews_common_stock(self):
        data={'name':'Володя Барабинск','location':'Барабинск','served_objects':[str(self.area.pk),str(self.second.pk)],'served_brigades':[str(self.crew.pk),str(self.crew2.pk)]}
        response=self.client.post(reverse('stock_new'),data);self.assertEqual(response.status_code,302)
        wh=Warehouse.objects.get(name=data['name']);self.assertEqual(wh.served_objects.count(),2);self.assertEqual(wh.served_brigades.count(),2)
        item=StockItem.objects.create(legal_entity=self.entity,sku='X',name='Материал',unit='pcs',created_by=self.leader)
        stock.move(self.leader,item.pk,'receipt',7,None,wh.pk,self.today,'Поступление',uuid.uuid4())
        data['expected_version']=wh.version;data['served_objects']=[str(self.second.pk)]
        self.assertEqual(self.client.post(reverse('stock_edit',args=[wh.pk]),data).status_code,302)
        self.assertEqual(StockBalance.objects.get(warehouse=wh,item=item).quantity,Decimal(7));self.assertEqual(StockMovement.objects.count(),1)
        response=self.client.get(reverse('object_detail',args=[self.second.pk]));self.assertContains(response,wh.name)
    def test_empty_catalogue_inline_receipt_atomic(self):
        wh=Warehouse.objects.create(legal_entity=self.entity,name='Склад',created_by=self.leader)
        response=self.client.get(reverse('stock_move'));self.assertContains(response,'Справочник материалов и муфт пока пуст')
        data={'token':str(uuid.uuid4()),'create_item':'on','kind':'receipt','quantity':'5','destination':str(wh.pk),'date':str(self.today),'note':'Поступление по накладной','new-sku':'ИЗОЛ','new-name':'Изоляционный материал','new-kind':'material','new-unit':'m'}
        response=self.client.post(reverse('stock_move'),data);self.assertEqual(response.status_code,302,str(response.context['form'].errors) if response.status_code!=302 else '')
        item=StockItem.objects.get(sku='ИЗОЛ');self.assertEqual(StockBalance.objects.get(item=item,warehouse=wh).quantity,5)
        data.update(token=str(uuid.uuid4()),kind='consume',source=str(wh.pk),destination='',**{'new-sku':'BAD'})
        response=self.client.post(reverse('stock_move'),data);self.assertEqual(response.status_code,409)
        self.assertFalse(StockItem.objects.filter(sku='BAD').exists());self.assertEqual(StockMovement.objects.count(),1)

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.conf import settings
class ResourceBrowser(StaticLiveServerTestCase):
    def test_pick_two_cars_and_shared_warehouse_and_inline_material(self):
        WorkflowTests.setUpTestData.__func__(type(self))
        second=ProjectObject.objects.create(legal_entity=self.entity,name='Барабинск–Татарская',status='active');ContractObject.objects.create(contract=self.contract,project_object=second)
        person=Person.objects.create(legal_entity=self.entity,full_name='Зарипов',created_by=self.leader)
        crews=[Brigade.objects.create(legal_entity=self.entity,name='Бригада '+str(i),foreman=person,created_by=self.leader) for i in [1,2]]
        cars=[Equipment.objects.create(legal_entity=self.entity,name='Соболь',category='Авто',inventory_number=str(i),registration_number='Р'+str(i),created_by=self.leader) for i in [721,992]]
        f={'contract':str(self.contract.pk),'areas':[str(self.area.pk),str(second.pk)],'crews':[str(b.pk) for b in crews],'cars':[str(e.pk) for e in cars]}
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/resources617-browser.cjs')],env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_TEST_FIXTURE':json.dumps(f)},capture_output=True,text=True,timeout=100)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(EquipmentAssignment.objects.count(),2)
        wh=Warehouse.objects.get(name='Володя Барабинск');self.assertEqual(wh.served_objects.count(),2);self.assertEqual(wh.served_brigades.count(),2)
        self.assertEqual(sum(StockBalance.objects.filter(warehouse=wh).values_list('quantity',flat=True)),2)
