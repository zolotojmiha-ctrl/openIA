from django.test import TestCase,override_settings
from django.urls import reverse
from . import tests as account_tests
from maxbot.models import Binding


@override_settings(ARMADA_MAX_ENABLED=True)
class MaxLoginPromptTests(TestCase):
    setUpTestData=classmethod(account_tests.AccountTests.setUpTestData.__func__)

    def enter(self,user):
        return self.client.post(reverse('login'),{'phone':user.phone,'password':self.secret})

    def test_unbound_foreman_and_leader_can_connect_or_defer_each_login(self):
        for user in (self.employee,self.first):
            self.client.logout()
            self.assertRedirects(self.enter(user),reverse('max_onboarding'))
            page=self.client.get(reverse('max_onboarding'))
            self.assertContains(page,'Подключить MAX');self.assertContains(page,'Позже')
            self.assertRedirects(self.client.post(reverse('max_onboarding'),{'action':'later'}),reverse('dashboard'))
            self.assertRedirects(self.client.get(reverse('max_onboarding')),reverse('dashboard'))
            self.assertEqual(self.client.get(reverse('dashboard')).status_code,200)
            self.client.post(reverse('logout'))
            self.assertRedirects(self.enter(user),reverse('max_onboarding'))
            self.assertRedirects(self.client.post(reverse('max_onboarding'),{'action':'connect'}),reverse('max_connection'))

    def test_current_binding_skips_prompt_but_stale_binding_does_not(self):
        b=Binding.objects.create(user=self.employee,max_user=97531,chat=97531,active=True,session_version=self.employee.session_version)
        self.assertRedirects(self.enter(self.employee),reverse('dashboard'))
        self.client.logout();b.session_version+=1;b.save()
        self.assertRedirects(self.enter(self.employee),reverse('max_onboarding'))

    @override_settings(ARMADA_MAX_ENABLED=False)
    def test_disabled_max_skips_prompt(self):
        self.assertRedirects(self.enter(self.employee),reverse('dashboard'))

    def test_unsupported_role_not_offered_unusable_connection(self):
        self.employee.role='mechanic';self.employee.save()
        self.assertRedirects(self.enter(self.employee),reverse('dashboard'))

    def test_direct_onboarding_without_login_marker_is_ignored(self):
        self.client.force_login(self.employee)
        self.assertRedirects(self.client.get(reverse('max_onboarding')),reverse('dashboard'))
