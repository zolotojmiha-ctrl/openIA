import os,subprocess,json,uuid
from datetime import timedelta
from django.conf import settings
from django.test import TestCase
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from . import test_stock as base
from . import stock,stock_groups as groups
from .models import StockItem,StockBalance,StockMovement,LegalEntity,COMPANY_TAX_ID

class Stock6201Tests(TestCase):
    setUpTestData=classmethod(base.StockTests.setUpTestData.__func__)
    wh=base.StockTests.wh;data=base.StockTests.data;item=base.StockTests.item;move=base.StockTests.move;balance=base.StockTests.balance
    def test_menu_opens_material_rows_and_warehouse_configuration_is_secondary(self):
        wh=self.wh('Володя Барабинск');i=self.item();self.move(i,q=10,dst=wh);self.client.force_login(self.leader)
        response=self.client.get(reverse('stock'),follow=True)
        self.assertTemplateUsed(response,'projects/stock_inventory.html');self.assertContains(response,wh.name);self.assertContains(response,'Изменить')
        self.assertContains(response,reverse('stock_warehouses'))
        self.assertTemplateUsed(self.client.get(reverse('stock_warehouses')),'projects/stock_home.html')
    def test_legacy_card_attach_uses_existing_position_and_returns_to_materials(self):
        wh=self.wh('Володя Барабинск');i=self.item(kind='coupling',unit='pcs',method='p1',diameter_mm=720,length_mm=1500,wall_mm=10);self.client.force_login(self.leader)
        response=self.client.get(reverse('stock'),follow=True);self.assertContains(response,'Указать склад и количество')
        form=self.client.get(reverse('stock_receive'),{'item':i.pk,'return_inventory':'1'}).context['form']
        self.assertEqual(form.initial['warehouse'].pk,wh.pk);self.assertEqual(form.initial['catalogue_item'].pk,i.pk)
        response=self.client.post(reverse('stock_receive'),{'return_inventory':'on','catalogue_item':i.pk,'catalogue_version':i.version,'warehouse':wh.pk,'quantity':1,'date':str(self.today),'note':'Уточнение начального остатка','token':uuid.uuid4(),'name':i.name,'kind':'coupling','unit':'pcs','method':'p1','diameter_mm':720,'length_mm':1500,'wall_mm':10,'serial':''})
        self.assertRedirects(response,reverse('stock_inventory'));self.assertEqual(StockItem.objects.count(),1);self.assertEqual(self.balance(wh,i),1)
    def test_edit_quantity_preserves_other_warehouse_and_stale_edit_is_rejected(self):
        a=self.wh('А');b=self.wh('Б');i=self.item();self.move(i,q=10,dst=a);self.move(i,'transfer',2,a,b);self.client.force_login(self.leader)
        g=next(g for g in groups.groups(self.leader) if g['warehouse'].pk==a.pk)
        url=reverse('stock_group_edit');response=self.client.get(url,{'group':g['token']});self.assertContains(response,'Изменить характеристики')
        response=self.client.post(url,{'group':g['token'],'quantity':'7','note':'Пересчёт только на А'})
        self.assertRedirects(response,reverse('stock_inventory'));self.assertEqual((self.balance(a,i),self.balance(b,i)),(7,2))
        self.assertEqual(StockMovement.objects.first().note,'Пересчёт только на А')
        self.client.post(url,{'group':g['token'],'quantity':'6','note':'Устаревшее сохранение'});self.assertEqual(self.balance(a,i),7)
    def test_readonly_warehouse_access_does_not_get_edit_links(self):
        from .models import WarehouseAccess
        wh=self.wh();i=self.item();self.move(i,q=1,dst=wh);WarehouseAccess.objects.create(warehouse=wh,user=self.worker,granted_by=self.leader)
        self.client.force_login(self.worker);response=self.client.get(reverse('stock'));self.assertEqual(response.status_code,200)
        self.assertNotContains(self.client.get(reverse('stock_detail',args=[wh.pk])),'>Изменить</a>')
        self.assertEqual(self.client.get(reverse('stock_group_edit')).status_code,403)

class Stock6201Browser(StaticLiveServerTestCase):
    def test_menu_legacy_material_attach_edit_and_quantity(self):
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'Тест'})
        base.StockTests.setUpTestData.__func__(type(self));self.leader.set_password('123456');self.leader.save()
        crew=base.StockTests.crew(self)
        wh=stock.save_warehouse(self.leader,None,None,{'name':'Володя Барабинск','brigade_assignment':crew})
        receipt_date=self.today-timedelta(days=72)
        item=stock.save_item(self.leader,None,None,{'sku':'LEGACY-6201','name':'Старая муфта','kind':'coupling','unit':'pcs','method':'p1','diameter_mm':720,'length_mm':1500,'wall_mm':10,'archived':False,'coupling':None})
        job=base.StockTests.make_job(self);job.method='p1';job.length_m='1.5';job.wall_mm=10;job.save()
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/stock6201-browser.cjs')],env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_STOCK6201':json.dumps({'warehouse':str(wh.pk),'item':str(item.pk),'job':str(job.pk),'receipt_date':str(receipt_date)})},capture_output=True,text=True,timeout=100)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        item.refresh_from_db();self.assertEqual(item.name,'Муфта исправлена');self.assertEqual(StockItem.objects.count(),1);self.assertEqual(StockBalance.objects.get(item=item,warehouse=wh).quantity,0)
        self.assertEqual(item.receipt_date,receipt_date)
