from django.test import TestCase
from django.urls import reverse
from .models import User,AuditEvent,Preference

class ProfileNameTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user('+70000000001','123456',full_name='ОшибкаОшибка',leader_slot=1,must_change_password=False)
        self.other=User.objects.create_user('+70000000002','123456',full_name='Другой',leader_slot=2,must_change_password=False)
        self.client.force_login(self.user)
    def snapshot(self):return self.client.get(reverse('profile')).context['name_form'].initial['snapshot']
    def test_fix_name_preserves_password_phone_permissions_and_sessions(self):
        version=self.user.session_version
        response=self.client.post(reverse('profile'),{'action':'identity','snapshot':self.snapshot(),'full_name':'Исправленное имя','phone':self.other.phone,'leader_slot':'2'})
        self.assertEqual(response.status_code,302);self.user.refresh_from_db();self.other.refresh_from_db()
        self.assertEqual(self.user.full_name,'Исправленное имя');self.assertEqual(self.user.phone,'+70000000001');self.assertEqual(self.user.leader_slot,1)
        self.assertTrue(self.user.check_password('123456'));self.assertEqual(self.user.session_version,version);self.assertEqual(self.other.full_name,'Другой')
        self.assertContains(self.client.get(reverse('profile')),'Исправленное имя')
        self.assertEqual(AuditEvent.objects.get(action='employee_updated').details['previous_name'],'ОшибкаОшибка')
    def test_blank_long_and_foreign_snapshot_rejected(self):
        snapshot=self.snapshot()
        for name in ['', 'x'*151]:
            self.client.post(reverse('profile'),{'action':'identity','snapshot':snapshot,'full_name':name})
        self.client.force_login(self.other)
        self.client.post(reverse('profile'),{'action':'identity','snapshot':snapshot,'full_name':'Чужое'})
        self.user.refresh_from_db();self.other.refresh_from_db()
        self.assertEqual(self.user.full_name,'ОшибкаОшибка');self.assertEqual(self.other.full_name,'Другой')
    def test_concurrent_change_does_not_overwrite_or_reset_preferences(self):
        snapshot=self.snapshot();Preference.objects.filter(user=self.user).update(compact=True)
        User.objects.filter(pk=self.user.pk).update(full_name='Новое имя')
        self.assertContains(self.client.post(reverse('profile'),{'action':'identity','snapshot':snapshot,'full_name':'Старый ввод'}),'уже изменилось')
        self.user.refresh_from_db();self.assertEqual(self.user.full_name,'Новое имя');self.assertTrue(Preference.objects.get(user=self.user).compact)
