import secrets,threading
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from django.db import connection,connections,close_old_connections
from django.test import TransactionTestCase
from django.core.exceptions import ValidationError
from django.utils import timezone
from accounts.models import User
from .models import LegalEntity,ProjectObject,Equipment,EquipmentAssignment,COMPANY_TAX_ID
from .equipment import save_equipment,assign

@skipUnless(connection.vendor=='postgresql','Проверка блокировок требует PostgreSQL')
class EquipmentConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.leaders=[User.objects.create_user(f'+7000000000{i}',secrets.token_urlsafe(24),leader_slot=i,full_name='ТЕСТ',must_change_password=False) for i in (1,2)]
        self.company=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})[0]
        self.objects=[ProjectObject.objects.create(legal_entity=self.company,name=f'Объект {i}') for i in (1,2)]
        self.data=dict(name='ТЕСТ Кран',category='Кран',inventory_number='0001',registration_number='',serial_number='',ownership='own',owner='',status='ready',notes='')
    def race(self,operation):
        barrier=threading.Barrier(2)
        def run(i):
            close_old_connections()
            try:barrier.wait(timeout=10);operation(i);return 'saved'
            except ValidationError:return 'conflict'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[0,1]))
        self.assertCountEqual(results,['saved','conflict'])
    def test_two_people_cannot_assign_same_machine_to_two_objects(self):
        item=save_equipment(self.leaders[0],None,None,self.data)
        self.race(lambda i:assign(self.leaders[i],item.pk,item.version,self.objects[i],None,timezone.localdate()))
        self.assertEqual(EquipmentAssignment.objects.count(),1);item.refresh_from_db();self.assertEqual(item.version,2)
    def test_simultaneous_duplicate_inventory_returns_validation_conflict(self):
        self.race(lambda i:save_equipment(self.leaders[i],None,None,self.data))
        self.assertEqual(Equipment.objects.count(),1)
