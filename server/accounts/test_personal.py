import secrets
from datetime import date
from django.test import TestCase
from django.urls import reverse
from .models import User,Preference,Reminder,SupportRequest

class PersonalSettingsTests(TestCase):
    def setUp(self):
        p=secrets.token_urlsafe(24)
        self.first=User.objects.create_user('+70000000003',p,full_name='ТЕСТ Пользователь',must_change_password=False)
        self.other=User.objects.create_user('+70000000004',p,full_name='ТЕСТ Другой',must_change_password=False)
        self.client.force_login(self.first)
    def test_preferences_are_personal_and_cannot_change_roles(self):
        self.client.post(reverse('profile'),{'hints':'','compact':'on','role':'accounting','leader_slot':1})
        prefs=Preference.objects.get(user=self.first);self.assertFalse(prefs.hints);self.assertTrue(prefs.compact)
        self.first.refresh_from_db();self.assertFalse(self.first.is_leader);self.assertEqual(self.first.role,'')
    def test_private_reminders(self):
        reminder=Reminder.objects.create(user=self.other,date=date.today(),text='ЧУЖОЕ НАПОМИНАНИЕ')
        self.assertNotContains(self.client.get(reverse('reminders')),reminder.text)
        self.assertEqual(self.client.get(reverse('reminder_edit',args=[reminder.pk])).status_code,404)
    def test_support_only_author_and_leaders_can_read(self):
        item=SupportRequest.objects.create(author=self.other,text='ЧУЖОЕ ОБРАЩЕНИЕ')
        self.assertNotContains(self.client.get(reverse('support')),item.text)
        self.assertEqual(self.client.get(reverse('support_detail',args=[item.pk])).status_code,404)
    def test_notification_preferences_do_not_remove_unread_count(self):
        self.client.post(reverse('profile'),{})
        response=self.client.get(reverse('notifications'))
        self.assertEqual(response.status_code,200);self.assertIn('inbox',response.json());self.assertIn('keys',response.json())
    def test_end_other_sessions_keeps_current_login(self):
        from django.test import Client
        other=Client();other.force_login(self.first)
        self.client.get(reverse('profile'));other.get(reverse('profile'))
        self.assertEqual(self.client.post(reverse('end_sessions'),{'all':'1'}).status_code,302)
        self.assertEqual(self.client.get(reverse('profile')).status_code,200)
        self.assertEqual(other.get(reverse('profile')).status_code,302)
