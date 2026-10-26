from django import forms
from django.contrib.auth.forms import PasswordChangeForm
from .models import User, ROLES, PERMISSIONS, normalize_phone

class LoginForm(forms.Form):
    phone=forms.CharField(label='Телефон',max_length=40,widget=forms.TextInput(attrs={'type':'tel','autocomplete':'username','placeholder':'+7 …'}))
    password=forms.CharField(label='Пароль',widget=forms.PasswordInput(attrs={'autocomplete':'current-password'}))
    def clean_phone(self):
        return normalize_phone(self.cleaned_data['phone'])

class EmployeeForm(forms.Form):
    expected_version=forms.IntegerField(required=False,widget=forms.HiddenInput)
    full_name=forms.CharField(label='Фамилия, имя и отчество',max_length=150)
    phone=forms.CharField(label='Телефон',max_length=40,widget=forms.TextInput(attrs={'type':'tel'}))
    role=forms.ChoiceField(label='Рабочая роль',choices=ROLES)
    is_active=forms.BooleanField(label='Учётная запись активна',required=False,initial=True)
    permissions=forms.MultipleChoiceField(label='Индивидуальные разрешения',choices=list(PERMISSIONS.items()),required=False,widget=forms.CheckboxSelectMultiple)
    def __init__(self,*args,user=None,actor=None,**kwargs):
        self.edit_user=user
        self.actor=actor
        super().__init__(*args,**kwargs)
        from postoffice.forms import add_request_fields
        add_request_fields(self,actor)
        if actor and not actor.is_leader:
            allowed=set(actor.grants.values_list('permission',flat=True))
            self.fields['permissions'].choices=[(k,v) for k,v in PERMISSIONS.items() if k in allowed]
    def clean(self):
        from postoffice.forms import validate_request
        return validate_request(self,super().clean())
    def clean_phone(self):
        phone=normalize_phone(self.cleaned_data['phone'])
        existing=User.objects.filter(phone=phone)
        if self.edit_user:
            existing=existing.exclude(pk=self.edit_user.pk)
        if existing.exists():
            raise forms.ValidationError('Этот телефон уже используется.')
        return phone

class SecurePasswordChangeForm(PasswordChangeForm):
    def clean(self):
        data=super().clean()
        value=data.get('new_password2')
        if value and self.user.check_password(value):
            self.add_error('new_password2','Новый пароль должен отличаться от текущего.')
        return data
