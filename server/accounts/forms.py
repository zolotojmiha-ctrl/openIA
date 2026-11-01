from django import forms
from django.contrib.auth.forms import PasswordChangeForm
from .presets import PRESETS
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
    role=forms.ChoiceField(label='Роль в ЛК (сама по себе не выдаёт права)',choices=ROLES)
    profile=forms.ChoiceField(label='Набор возможностей',choices=[('custom','Индивидуальная настройка')]+[(k,v['label']) for k,v in PRESETS.items()],required=False,help_text='Права выбранного набора включаются при сохранении. Галочки ниже — дополнительные разрешения. Доступ к конкретным контрактам, участкам и файлам выдаётся отдельно.')
    is_active=forms.BooleanField(label='Учётная запись активна',required=False,initial=True)
    permissions=forms.MultipleChoiceField(label='Индивидуальные разрешения',choices=list(PERMISSIONS.items()),required=False,widget=forms.CheckboxSelectMultiple)
    def __init__(self,*args,user=None,actor=None,**kwargs):
        self.edit_user=user
        self.actor=actor
        super().__init__(*args,**kwargs)
        from projects.models import ContractObject,CustomerAccess
        from projects.access import visible_contracts,visible_objects
        scopes=ContractObject.objects.filter(contract__in=visible_contracts(actor),project_object__in=visible_objects(actor)).select_related('contract','project_object') if actor and actor.is_leader else ContractObject.objects.none()
        self.fields['customer_scopes_present']=forms.BooleanField(required=False,initial=True,widget=forms.HiddenInput)
        self.fields['customer_scopes']=forms.ModelMultipleChoiceField(label='Контракты и участки заказчика',queryset=scopes,required=False,widget=forms.CheckboxSelectMultiple)
        self.fields['customer_scopes'].label_from_instance=lambda s:f'{s.contract.number} · {s.project_object.name}'
        if user and user.role=='customer':
            old=set(CustomerAccess.objects.filter(user=user).values_list('contract_id','project_object_id'))
            self.initial['customer_scopes']=[s.pk for s in scopes if (s.contract_id,s.project_object_id) in old]
        from postoffice.forms import add_request_fields
        add_request_fields(self,actor)
        if actor and not actor.is_leader:
            allowed_profiles=[('custom','Индивидуальная настройка')]+[(k,v['label']) for k,v in PRESETS.items() if k!='customer' and set(v['permissions']).issubset(set(actor.grants.values_list('permission',flat=True)))]
            self.fields['profile'].choices=allowed_profiles
            self.fields['role'].choices=[v for v in ROLES if v[0]!='customer']
            allowed=set(actor.grants.values_list('permission',flat=True))
            self.fields['permissions'].choices=[(k,v) for k,v in PERMISSIONS.items() if k in allowed]
    def clean(self):
        from postoffice.forms import validate_request
        data=super().clean()
        profile=PRESETS.get(data.get('profile'))
        if profile:data['permissions']=list(set(data.get('permissions',[]))|set(profile['permissions']))
        if data.get('role')=='customer':
            if not self.actor or not self.actor.is_leader:raise forms.ValidationError('Заказчика создаёт руководитель.')
            if data.get('permissions') or data.get('request_mail'):raise forms.ValidationError('Заказчику не выдаются внутренние права и корпоративная почта. Выберите набор заказчика без внутренних прав.')
        if data.get('role')!='customer' and data.get('profile')=='customer':raise forms.ValidationError('Набор заказчика применяется только к роли «Заказчик».')
        return validate_request(self,data)
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
