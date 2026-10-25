import secrets
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from .models import User, AccessGrant, AuditEvent, LoginAttempt, normalize_phone

class AccountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.secret=secrets.token_urlsafe(24)
        cls.first=User.objects.create_user('+70000000001',cls.secret,full_name='ТЕСТ Руководитель 1',leader_slot=1,must_change_password=False)
        cls.second=User.objects.create_user('+70000000002',cls.secret,full_name='ТЕСТ Руководитель 2',leader_slot=2,must_change_password=False)
        cls.employee=User.objects.create_user('+70000000003',cls.secret,full_name='ТЕСТ Мастер',role='foreman',must_change_password=False)
    def leader_login(self,second=False):
        self.client.force_login(self.second if second else self.first)
    def employee_data(self,**extra):
        return {'expected_version':'1','full_name':'ТЕСТ Новый сотрудник','phone':'+70000000004','role':'foreman','is_active':'on',**extra}
    def test_anonymous_cannot_view_any_private_page(self):
        for path in ['/cabinet/','/cabinet/users/','/cabinet/audit/','/cabinet/users/new/']:
            self.assertEqual(self.client.get(path).status_code,302)
    def test_login_by_normalized_phone(self):
        response=self.client.post(reverse('login'),{'phone':'+7 (000) 000-00-03','password':self.secret})
        self.assertRedirects(response,reverse('dashboard'))
    def test_incorrect_and_unknown_credentials_have_same_message(self):
        for phone in [self.employee.phone,'+70000000009']:
            r=self.client.post(reverse('login'),{'phone':phone,'password':'wrong'})
            self.assertContains(r,'Не удалось войти.')
        self.assertNotIn('_auth_user_id',self.client.session)
    def test_inactive_user_cannot_login(self):
        self.employee.is_active=False;self.employee.save()
        r=self.client.post(reverse('login'),{'phone':self.employee.phone,'password':self.secret})
        self.assertContains(r,'Не удалось войти.')
    def test_bruteforce_limit(self):
        for _ in range(8):
            self.client.post(reverse('login'),{'phone':self.employee.phone,'password':'wrong'})
        r=self.client.post(reverse('login'),{'phone':self.employee.phone,'password':self.secret})
        self.assertContains(r,'Слишком много попыток')
        self.assertNotIn('_auth_user_id',self.client.session)
    def test_rate_limit_expires(self):
        for _ in range(8):
            self.client.post(reverse('login'),{'phone':self.employee.phone,'password':'wrong'})
        LoginAttempt.objects.update(window_start=timezone.now()-timedelta(minutes=16))
        self.assertEqual(self.client.post(reverse('login'),{'phone':self.employee.phone,'password':self.secret}).status_code,302)
    def test_temporary_password_forces_change_and_blocks_management(self):
        self.first.must_change_password=True;self.first.save()
        self.client.post(reverse('login'),{'phone':self.first.phone,'password':self.secret})
        self.assertRedirects(self.client.get(reverse('users')),reverse('password'))
    def test_expired_temporary_password_cannot_login(self):
        self.employee.must_change_password=True
        self.employee.temporary_expires_at=timezone.now()-timedelta(seconds=1)
        self.employee.save()
        r=self.client.post(reverse('login'),{'phone':self.employee.phone,'password':self.secret})
        self.assertContains(r,'Не удалось войти.')
    def test_change_password_finishes_other_sessions(self):
        other=Client();other.force_login(self.employee)
        self.client.force_login(self.employee)
        new=secrets.token_urlsafe(24)
        r=self.client.post(reverse('password'),{'old_password':self.secret,'new_password1':new,'new_password2':new})
        self.assertRedirects(r,reverse('dashboard'))
        self.employee.refresh_from_db();self.assertTrue(self.employee.check_password(new))
        self.assertFalse(self.employee.must_change_password)
        self.assertEqual(other.get(reverse('dashboard')).status_code,302)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code,200)
    def test_short_password_rejected(self):
        self.client.force_login(self.employee)
        self.client.post(reverse('password'),{'old_password':self.secret,'new_password1':'1234','new_password2':'1234'})
        self.employee.refresh_from_db();self.assertTrue(self.employee.check_password(self.secret))
    def test_same_password_rejected(self):
        self.client.force_login(self.employee)
        self.assertContains(self.client.post(reverse('password'),{'old_password':self.secret,'new_password1':self.secret,'new_password2':self.secret}),'должен отличаться')
    def test_csrf_is_required_on_login(self):
        c=Client(enforce_csrf_checks=True)
        self.assertEqual(c.post(reverse('login'),{'phone':self.first.phone,'password':self.secret}).status_code,403)
    def test_csrf_is_required_on_management(self):
        c=Client(enforce_csrf_checks=True);c.force_login(self.first)
        self.assertEqual(c.post(reverse('employee_new'),self.employee_data()).status_code,403)
    def test_employee_cannot_manage_users_even_by_direct_request(self):
        self.client.force_login(self.employee)
        for path in [reverse('users'),reverse('audit'),reverse('employee_new'),reverse('employee_edit',args=[self.first.pk]),reverse('employee_reset',args=[self.second.pk])]:
            self.assertEqual(self.client.post(path,self.employee_data()).status_code,403)
    def test_both_leaders_can_create_and_set_permissions(self):
        for index,leader in enumerate([self.first,self.second]):
            self.client.force_login(leader)
            phone='+7000000000'+str(4+index)
            r=self.client.post(reverse('employee_new'),self.employee_data(phone=phone,permissions=['document.upload','personnel.manage'],leader_slot='1',is_superuser='on'))
            self.assertEqual(r.status_code,200)
            user=User.objects.get(phone=phone)
            self.assertFalse(user.is_leader)
            self.assertEqual(set(user.grants.values_list('permission',flat=True)),{'document.upload','personnel.manage'})
            self.assertTrue(user.must_change_password)
            temporary=r.context['temporary'];self.assertEqual(len(temporary),6);self.assertTrue(user.check_password(temporary))
            self.assertNotEqual(user.password,temporary)
            self.assertNotIn(temporary,str(list(AuditEvent.objects.values())))
    def test_no_default_grants(self):
        self.leader_login();self.client.post(reverse('employee_new'),self.employee_data())
        self.assertEqual(User.objects.get(phone='+70000000004').grants.count(),0)
    def test_blocking_user_invalidates_active_session(self):
        employee_client=Client();employee_client.force_login(self.employee)
        self.leader_login()
        self.client.post(reverse('employee_edit',args=[self.employee.pk]),self.employee_data(phone=self.employee.phone,is_active=''))
        self.assertEqual(employee_client.get(reverse('dashboard')).status_code,302)
    def test_permission_change_revokes_sessions_and_is_audited(self):
        employee_client=Client();employee_client.force_login(self.employee)
        self.leader_login();self.client.post(reverse('employee_edit',args=[self.employee.pk]),self.employee_data(phone=self.employee.phone,permissions=['document.upload']))
        self.assertEqual(employee_client.get(reverse('dashboard')).status_code,302)
        self.assertTrue(AuditEvent.objects.filter(action='employee_updated',details__permissions=['document.upload']).exists())
    def test_reset_password_revokes_sessions(self):
        employee_client=Client();employee_client.force_login(self.employee)
        self.leader_login();r=self.client.post(reverse('employee_reset',args=[self.employee.pk]))
        self.employee.refresh_from_db();self.assertEqual(len(r.context['temporary']),6);self.assertTrue(self.employee.check_password(r.context['temporary']))
        self.assertTrue(self.employee.must_change_password)
        self.assertEqual(employee_client.get(reverse('dashboard')).status_code,302)
    def test_no_registration_or_django_admin(self):
        for path in ['/register/','/cabinet/register/','/admin/']:
            self.assertEqual(self.client.get(path).status_code,404)
    def test_leader_accounts_cannot_be_created_via_employee_editor(self):
        self.leader_login()
        self.assertEqual(self.client.get(reverse('employee_edit',args=[self.second.pk])).status_code,404)
    def test_database_prevents_third_leader(self):
        with self.assertRaises(IntegrityError),transaction.atomic():
            User.objects.create_user('+70000000008',self.secret,full_name='ТЕСТ',leader_slot=3)
    def test_duplicate_phone_normalization(self):
        self.leader_login()
        self.assertContains(self.client.post(reverse('employee_new'),self.employee_data(phone='+7 (000) 000-00-03')),'Этот телефон уже используется')
    def test_logout_requires_post(self):
        self.leader_login();self.assertEqual(self.client.get(reverse('logout')).status_code,405)
        self.client.post(reverse('logout'));self.assertEqual(self.client.get(reverse('dashboard')).status_code,302)
    def test_no_store_on_private_pages(self):
        self.leader_login();r=self.client.get(reverse('users'))
        self.assertEqual(r['Cache-Control'],'no-store, private')
        c=Client()
        login_response=c.post(reverse('login'),{'phone':self.first.phone,'password':self.secret})
        self.assertTrue(login_response.cookies['armada_session']['httponly'])
    def test_permission_allowlist(self):
        self.leader_login();r=self.client.post(reverse('employee_new'),self.employee_data(permissions=['document.approve']))
        self.assertEqual(r.status_code,200);self.assertFalse(User.objects.filter(phone='+70000000004').exists())
    def test_name_is_escaped(self):
        self.employee.full_name='<script>alert(1)</script>';self.employee.save()
        self.leader_login();r=self.client.get(reverse('users'))
        self.assertContains(r,'&lt;script&gt;');self.assertNotContains(r,'<script>alert(1)</script>')
    def test_bootstrap_refuses_noninteractive_execution(self):
        with patch('sys.stdin.isatty',return_value=False),self.assertRaises(CommandError):
            call_command('bootstrap_leaders',stdout=StringIO())
    def test_bootstrap_rejects_short_second_password_atomically(self):
        User.objects.all().delete()
        values=['ТЕСТ Первый','+70000000001',self.secret,self.secret,'ТЕСТ Второй','+70000000002','12345','12345']
        with patch('sys.stdin.isatty',return_value=True),patch('getpass.getpass',side_effect=values),self.assertRaises(CommandError):
            call_command('bootstrap_leaders',stdout=StringIO())
        self.assertEqual(User.objects.count(),0)
    def test_bootstrap_creates_exactly_two_and_cannot_repeat(self):
        User.objects.all().delete()
        other=secrets.token_urlsafe(24)
        values=['ТЕСТ Первый','+70000000001',self.secret,self.secret,'ТЕСТ Второй','+70000000002',other,other]
        with patch('sys.stdin.isatty',return_value=True),patch('getpass.getpass',side_effect=values):
            call_command('bootstrap_leaders',stdout=StringIO())
        self.assertEqual(User.objects.filter(leader_slot__isnull=False).count(),2)
        with patch('sys.stdin.isatty',return_value=True),self.assertRaises(CommandError):
            call_command('bootstrap_leaders',stdout=StringIO())
    def test_static_server_does_not_expose_database_or_source(self):
        for path in ['/.runtime/development.sqlite3','/server/config/settings.py','/portal/portal.js/../../.runtime/secret-key']:
            self.assertEqual(self.client.get(path).status_code,404)

    def test_stale_employee_settings_cannot_overwrite_other_leader(self):
        self.leader_login()
        url=reverse('employee_edit',args=[self.employee.pk])
        self.client.post(url,self.employee_data(phone=self.employee.phone,permissions=['document.upload']))
        response=self.client.post(url,self.employee_data(phone=self.employee.phone,permissions=['document.export']))
        self.assertContains(response,'Настройки уже изменил другой руководитель')
        self.assertEqual(list(self.employee.grants.values_list('permission',flat=True)),['document.upload'])
