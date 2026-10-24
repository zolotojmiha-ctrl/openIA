from django.test import TestCase
from django.urls import reverse
from accounts.models import User

class DraftPageTests(TestCase):
    def test_login_required(self):
        self.assertEqual(self.client.get(reverse('repair_drafts')).status_code,302)

    def test_private_browser_page_does_not_export_server_work(self):
        user=User.objects.create_user('+70000000951','test-secret',full_name='ТЕСТ Мастер',role='foreman',must_change_password=False)
        self.client.force_login(user)
        response=self.client.get(reverse('repair_drafts'))
        self.assertContains(response,'Черновики на этом устройстве')
        self.assertContains(response,str(user.pk))
        self.assertIn('no-store',response['Cache-Control'])
        self.assertNotContains(response,'data-repair-draft=')
