import uuid
from django.test import TestCase,override_settings
from django.utils import timezone
from django.db import transaction
from accounts.models import AccessGrant,AuditEvent
from projects import test_equipment as fixture,equipment_parts
from projects.models import EquipmentAssignment,EquipmentIssue
from . import links,worker
from .tests import CONFIG
from .models import Inbox,Outbox


@override_settings(**CONFIG)
class MechanicMenuTests(TestCase):
    setUpTestData=classmethod(fixture.EquipmentTests.setUpTestData.__func__)
    grant=fixture.EquipmentTests.grant
    make=fixture.EquipmentTests.make;data=fixture.EquipmentTests.data

    def setUp(self):
        code=links.issue(self.mechanic)
        with transaction.atomic():links.request_link(links.digest(code),101,201)
        self.binding=links.approve(self.mechanic,links.digest(code))

    def event(self,text):
        row=Inbox.objects.create(key=uuid.uuid4().hex,max_user=101,chat=201,payload={'text':text})
        worker.process(row.pk)
        return Outbox.objects.get(key=f'in:{row.pk}:reply')

    def test_mechanic_binding_welcome_and_menu_without_implicit_grants(self):
        welcome=Outbox.objects.get(binding=self.binding)
        self.assertIn('меню механика',welcome.body['text']);self.assertTrue(worker.delivery_allowed(welcome))
        row=self.event('/menu');self.assertIn('Меню механика',row.body['text'])
        self.assertNotIn('Добавить технику',str(row.body))
        self.assertNotIn('Аренда',str(row.body));self.assertNotIn('Отчёты на проверку',str(row.body))
        self.assertFalse(AccessGrant.objects.filter(user=self.mechanic).exists())
        self.grant();self.assertIn('Добавить технику',str(self.event('/menu').body))

    def test_parts_and_equipment_scope_and_role_recheck(self):
        self.grant();item=self.make()
        equipment_parts.save(self.mechanic,item.pk,None,0,dict(name='Фильтр',article='00123',manufacturer='',quantity=None,unit='шт.',note='',archived=False),uuid.uuid4())
        row=self.event('mechanic:parts:0')
        self.assertIn('#maintenance-parts',str(row.body));self.assertTrue(worker.delivery_allowed(row))
        AccessGrant.objects.filter(user=self.mechanic).delete()
        self.assertFalse(worker.delivery_allowed(row))
        self.assertNotIn(str(item.pk),str(self.event('mechanic:equipment:0').body))
        self.mechanic.role='foreman';self.mechanic.save()
        self.assertFalse(worker.delivery_allowed(Outbox.objects.get(key='binding:'+str(self.binding.pk))))

    def test_leader_and_foreman_commands_cannot_escape_mechanic_menu(self):
        for text in ('leader:rentals:0','/фото','/работы','/start'):
            row=self.event(text)
            self.assertIn('Меню механика',row.body['text']);self.assertTrue(row.scope['mechanic'])

    def test_notifications_only_equipment_and_authorized_issues(self):
        self.grant();item=self.make(status='repair')
        notice=Outbox.objects.get(scope__mechanic_event__isnull=False)
        self.assertTrue(worker.delivery_allowed(notice));self.assertIn('требует ремонта',notice.body['text'])
        placement=EquipmentAssignment.objects.create(equipment=item,project_object=self.a,contract=self.contract,start_date=self.today,created_by=self.leader)
        issue=EquipmentIssue.objects.create(equipment=item,placement=placement,description='Не заводится',author=self.worker,token=uuid.uuid4())
        event=AuditEvent.objects.create(actor=self.worker,action='equipment_issue',details={'issue':str(issue.pk)})
        notice=Outbox.objects.get(scope__mechanic_event=event.pk)
        self.assertIn('Не заводится',notice.body['text']);self.assertNotIn(self.contract.number,notice.body['text'])
        self.assertTrue(worker.delivery_allowed(notice))
        issue.state='closed';issue.save();self.assertFalse(worker.delivery_allowed(notice))
        count=Outbox.objects.count()
        AuditEvent.objects.create(actor=self.worker,action='repair_prepared',details={'kind':'material_request'})
        AuditEvent.objects.create(actor=self.worker,action='repair_reported',details={'kind':'note'})
        self.assertEqual(Outbox.objects.count(),count)

    def test_existing_max_identity_cannot_bind_another_employee(self):
        from django.core.exceptions import ValidationError
        code=links.issue(self.worker)
        with transaction.atomic():links.request_link(links.digest(code),101,201)
        with self.assertRaises(ValidationError):links.approve(self.worker,links.digest(code))
        self.binding.refresh_from_db();self.assertTrue(self.binding.active)
