from io import StringIO
from unittest.mock import patch
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from .forms import SecurePasswordChangeForm
from .models import User
from .passwords import temporary_password

class PasswordPolicyTests(TestCase):
    def test_five_rejected_six_accepted(self):
        with self.assertRaises(ValidationError):validate_password('Q7mVz')
        validate_password('123456')
        validate_password('abcdef',User(full_name='abcdef',phone='+70000000001'))
    def test_bootstrap_accepts_two_six_character_passwords(self):
        values=['Первый руководитель','+70000000001','123456','123456','Второй руководитель','+70000000002','123456','123456']
        with patch('sys.stdin.isatty',return_value=True),patch('getpass.getpass',side_effect=values) as prompt:
            call_command('bootstrap_leaders',stdout=StringIO())
        self.assertTrue(User.objects.get(leader_slot=1).check_password('123456'))
        self.assertTrue(User.objects.get(leader_slot=2).check_password('123456'))
        self.assertTrue(any('6 символов' in c.args[0] for c in prompt.call_args_list))
    def test_password_change_and_login_with_six_characters(self):
        user=User.objects.create_user('+70000000001','Q7mVz2',full_name='Сотрудник',must_change_password=False)
        form=SecurePasswordChangeForm(user,data={'old_password':'Q7mVz2','new_password1':'123456','new_password2':'123456'})
        self.assertTrue(form.is_valid(),form.errors);form.save()
        self.assertEqual(self.client.post(reverse('login'),{'phone':user.phone,'password':'123456'}).status_code,302)
        user.refresh_from_db();self.assertTrue(user.check_password('123456'))
    def test_generated_temporary_passwords_are_six_and_valid(self):
        for _ in range(20):
            value=temporary_password();self.assertEqual(len(value),6);self.assertTrue(value.isdecimal());validate_password(value)
