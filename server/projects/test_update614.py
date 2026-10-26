from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from accounts.models import User,AuditEvent
from accounts.business_notices import events
from documents.models import Document,Person
from .models import LegalEntity,COMPANY_TAX_ID,Contract,ProjectObject,ContractObject,CounterpartyAgreement,Equipment,EquipmentDocument
from . import agreements
from repairs.excel import workbook

class UpdateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.leader=User.objects.create_user('+79999980101','123456',full_name='Тест Руководитель',leader_slot=1,must_change_password=False)
        cls.worker=User.objects.create_user('+79999980102','123456',full_name='Тест Мастер',role='foreman',must_change_password=False)
        cls.company=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
        cls.contract=Contract.objects.create(legal_entity=cls.company,number='Заказчик-1',subject='Ремонт',work_kinds=['repairs'])
        cls.obj=ProjectObject.objects.create(legal_entity=cls.company,name='Объект-1');ContractObject.objects.create(contract=cls.contract,project_object=cls.obj)
    def data(self,**kw):return {'number':'Аренда-1','kind':'housing','counterparty':'Арендодатель','subject':'Проживание бригады','amount':'12000.00','expected_version':0,'customer_contract':str(self.contract.pk),'project_object':str(self.obj.pk),**kw}
    def test_agreement_separate_from_customer_and_history_preserved(self):
        row=agreements.save(self.leader,None,self.data())
        self.assertEqual(Contract.objects.count(),1);self.assertEqual(row.customer_contract,self.contract)
        agreements.save(self.leader,row.pk,self.data(expected_version=1,number='Аренда-2',archived=True))
        row.refresh_from_db();self.assertTrue(row.archived);self.assertEqual(row.number,'Аренда-2')
        with self.assertRaises(ValidationError):agreements.save(self.leader,row.pk,self.data(expected_version=1))
        self.assertEqual(AuditEvent.objects.filter(action='agreement_updated').count(),2)
        agreements.save(self.leader,row.pk,self.data(expected_version=2));row.refresh_from_db();self.assertFalse(row.archived)
    def test_agreement_wrong_object_negative_amount_and_dates_rejected(self):
        other=ProjectObject.objects.create(legal_entity=self.company,name='Другой объект')
        for data in [self.data(project_object=str(other.pk)),self.data(amount='-1'),self.data(date='2026-10-26',deadline='2026-10-25')]:
            with self.assertRaises(ValidationError):agreements.save(self.leader,None,data)
        self.assertFalse(CounterpartyAgreement.objects.exists())
    def test_agreement_permissions_and_views(self):
        self.client.force_login(self.worker)
        for route in ['agreements','agreement_new','agreement_excel']:self.assertEqual(self.client.get(reverse(route)).status_code,403)
        self.client.force_login(self.leader)
        row=agreements.save(self.leader,None,self.data())
        for route,args in [('agreements',[]),('agreement_detail',[row.pk]),('agreement_edit',[row.pk])]:self.assertEqual(self.client.get(reverse(route,args=args)).status_code,200)
        self.assertContains(self.client.get(reverse('contracts')),'Контракты с заказчиками')
        self.assertContains(self.client.get(reverse('work_home')),'Выполнение работ')
    def test_agreement_excel_preview_rollback_apply_and_replay(self):
        self.client.force_login(self.leader)
        cells=['','', 'А-2','equipment_rent','Поставщик','Аренда машины','','','1000',str(self.contract.pk),str(self.obj.pk),'','','0']
        f=SimpleUploadedFile('agreements.xlsx',workbook([cells],headers=agreements.HEADERS))
        response=self.client.post(reverse('agreement_excel'),{'file':f})
        self.assertFalse(CounterpartyAgreement.objects.exists());token=response.context['preview'];self.assertTrue(token,response.context['form'].errors)
        for _ in range(2):self.assertEqual(self.client.post(reverse('agreement_excel'),{'apply':'1','preview':token}).status_code,302)
        self.assertEqual(CounterpartyAgreement.objects.count(),1)
        r=self.client.get(reverse('agreement_excel')+'?export=1');self.assertEqual(r.status_code,200);self.assertGreater(len(r.content),100)
    def test_mail_noise_hidden_but_failures_and_audit_retained(self):
        for action,details in [('mail_prepared',{}),('mailbox_updated',{}),('mail_sent',{'state':'sent'}),('mail_sent',{'state':'failed'}),('mail_sent',{'state':'uncertain'}),('repair_reported',{})]:AuditEvent.objects.create(actor=self.leader,action=action,details=details)
        remaining=list(events(self.leader).values_list('action',flat=True))
        self.assertEqual(remaining.count('mail_sent'),2);self.assertNotIn('mail_prepared',remaining);self.assertNotIn('mailbox_updated',remaining)
        self.assertEqual(AuditEvent.objects.count(),6)
    def test_person_without_account_still_has_documents_and_no_login(self):
        self.client.force_login(self.leader)
        r=self.client.post(reverse('person_new'),{'full_name':'Работник без ЛК','position':'Монтажник'})
        self.assertEqual(r.status_code,302);self.assertIsNone(Person.objects.get(full_name='Работник без ЛК').user_id)
        self.assertEqual(User.objects.count(),2)
        self.assertContains(self.client.get(reverse('employee_new')),'Мастеру / прорабу')
    def test_mail_badges_and_read_visual_states(self):
        from postoffice.models import Mailbox
        box=Mailbox.objects.create(address='info@armada-sibir.ru',created_by=self.leader,state='active',unread=3)
        self.client.force_login(self.leader)
        self.assertContains(self.client.get(reverse('mail')),'Непрочитанных: 3')
        data=self.client.get(reverse('notifications')).json();self.assertEqual(data['mailboxes'][str(box.pk)],3)
        self.client.force_login(self.worker);self.assertNotIn(str(box.pk),self.client.get(reverse('notifications')).json()['mailboxes'])
    def test_equipment_document_rows_show_distinct_deadline_statuses(self):
        item=Equipment.objects.create(legal_entity=self.company,name='Машина',category='Авто',inventory_number='614',created_by=self.leader)
        for i,days in enumerate([-1,5,100,None]):
            doc=Document.objects.create(title=f'Документ {i}',legal_entity=self.company,section='other',created_by=self.leader)
            EquipmentDocument.objects.create(equipment=item,document=doc,label=doc.title,created_by=self.leader,expires_on=timezone.localdate()+timedelta(days=days) if days is not None else None)
        self.client.force_login(self.leader);r=self.client.get(reverse('equipment_detail',args=[item.pk]));self.assertEqual(r.status_code,200)
        for text in ['Просрочен','Скоро истекает','Действует','Без срока','equipment-doc-row']:self.assertContains(r,text)

    def test_mark_read_updates_server_flag_and_mailbox_counter(self):
        from postoffice.models import Mailbox
        from unittest.mock import MagicMock
        from contextlib import contextmanager
        box=Mailbox.objects.create(address='read@armada-sibir.ru',created_by=self.leader,state='active',unread=1)
        client=MagicMock();client.uid.return_value=('OK',[b''])
        @contextmanager
        def connect(_):yield client
        self.client.force_login(self.leader)
        with patch('postoffice.transport.connect',connect),patch('postoffice.transport.select'):
            r=self.client.post(reverse('mail_message_action',args=[box.pk,5]),{'action':'seen','folder':'INBOX'},HTTP_ACCEPT='application/json')
        self.assertEqual(r.status_code,200);box.refresh_from_db();self.assertEqual(box.unread,0)
        client.uid.assert_any_call('STORE','5','+FLAGS','(\\Seen)')
