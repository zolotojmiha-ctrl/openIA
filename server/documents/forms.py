from django import forms
from accounts.models import User
from projects.access import visible_objects,visible_contracts,contract_card
from .access import visible_people,visible_bundles,can_manage_bundle,sharing_users
from .models import Document, Bundle, Person, PersonAssignment
from .storage import validate_upload

class ContractChoice(forms.ModelMultipleChoiceField):
    def __init__(self,*args,user,**kwargs):self.user=user;super().__init__(*args,**kwargs)
    def label_from_instance(self,obj):return contract_card(obj,self.user)['title']

class LinkForm(forms.ModelForm):
    expected_revision=forms.IntegerField(widget=forms.HiddenInput,required=False)
    linked_objects=forms.ModelMultipleChoiceField(label='Объекты',queryset=None,required=False,widget=forms.CheckboxSelectMultiple)
    linked_people=forms.ModelMultipleChoiceField(label='Работники',queryset=Person.objects.none(),required=False,widget=forms.CheckboxSelectMultiple)
    confirm_access=forms.BooleanField(label='Я проверил список получателей при смене комплекта',required=False)
    class Meta:
        model=Document
        fields=['title','section','number','date','bundle','is_primary','link_not_required']
        widgets={'date':forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'})}
        labels={'section':'Раздел','title':'Название','bundle':'Комплект','is_primary':'Основной документ комплекта','number':'Номер','date':'Дата документа','link_not_required':'Связь не требуется'}
    def __init__(self,*args,user,**kwargs):
        self.user=user;super().__init__(*args,**kwargs)
        self.fields['title'].required=False
        self.fields['section'].initial='general'
        self.fields['linked_objects'].queryset=visible_objects(user).order_by('name')
        self.fields['linked_contracts']=ContractChoice(label='Договоры',queryset=visible_contracts(user).order_by('id'),user=user,required=False,widget=forms.CheckboxSelectMultiple)
        self.fields['linked_people'].queryset=visible_people(user).filter(archived=False).order_by('full_name')
        self.fields['bundle'].queryset=visible_bundles(user).filter(created_by=user) if not user.is_leader else visible_bundles(user)
        if not self.instance._state.adding:
            self.fields['title'].required=True
        self.fields['bundle'].required=False
    def clean(self):
        data=super().clean()
        if data.get('is_primary') and not (data.get('bundle') or data.get('bundle_title')):self.add_error('is_primary','Основной файл выбирается только внутри комплекта.')
        return data

class UploadForm(LinkForm):
    upload_token=forms.UUIDField(required=False,widget=forms.HiddenInput)
    file=forms.FileField(label='Файл — до 10 МБ')
    comment=forms.CharField(label='Комментарий',max_length=2000,required=False)
    bundle_title=forms.CharField(label='Или создать новый комплект',max_length=200,required=False)
    recipients=forms.ModelMultipleChoiceField(label='Открыть доступ сотрудникам (необязательно)',queryset=User.objects.none(),required=False,widget=forms.CheckboxSelectMultiple)
    collision=forms.ChoiceField(label='При совпадении имён',choices=[('ask','Спросить'),('replace','Заменить новой версией'),('both','Оставить оба')],initial='ask')
    collision_id=forms.UUIDField(required=False,widget=forms.HiddenInput)
    defer_link=forms.BooleanField(label='Сохранить без связи во «На разбор»',required=False)
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,user=user,**kwargs)
        self.fields['recipients'].queryset=sharing_users(user).filter(is_active=True).exclude(pk=user.pk).order_by('full_name')
        self.fields['recipients'].label_from_instance=lambda u:u.full_name
    def clean_file(self):
        value=self.cleaned_data['file'];self.file_data=validate_upload(value);return value

class VersionForm(forms.Form):
    expected_revision=forms.IntegerField(widget=forms.HiddenInput)
    file=forms.FileField(label='Новый файл — до 10 МБ')
    comment=forms.CharField(label='Что изменено (необязательно)',max_length=2000,required=False,widget=forms.Textarea)
    def clean_file(self):
        value=self.cleaned_data['file'];self.file_data=validate_upload(value);return value

class ShareForm(forms.Form):
    permission=forms.ChoiceField(label='Разрешение',choices=[('view','Просмотр и скачивание'),('versions','Просмотр и добавление версий'),('closed','Закрыть доступ')],required=False,initial='view')
    expected_revision=forms.IntegerField(widget=forms.HiddenInput)
    employee=forms.ModelChoiceField(label='Сотрудник',queryset=User.objects.none())
    enabled=forms.BooleanField(label='Просмотр и скачивание',required=False,initial=True)
    can_replace=forms.BooleanField(label='Также разрешить новые версии',required=False)
    def __init__(self,*args,document=None,actor=None,**kwargs):
        super().__init__(*args,**kwargs)
        from django.db.models import Q
        self.actor=actor;self.document=document
        qs=sharing_users(actor,document) if actor is not None else User.objects.none()
        if document is not None:
            qs=qs.filter(Q(is_active=True)|Q(pk__in=document.access_rules.values('user_id'))).exclude(pk__in=[value for value in [document.created_by_id,getattr(document,'responsible_id',None)] if value])
        else:qs=qs.filter(is_active=True)
        self.fields['employee'].queryset=qs.order_by('full_name')
        self.fields['employee'].label_from_instance=lambda u:u.full_name+(' (вход заблокирован)' if not u.is_active else '')
        self.fields['permission'].initial='closed' if self.initial.get('enabled') is False else 'versions' if self.initial.get('can_replace') else 'view'
    def clean(self):
        data=super().clean()
        if 'permission' in self.data:
            permission=data.get('permission')
            if not permission:self.add_error('permission','Выберите разрешение.')
            data['enabled']=permission!='closed'
            data['can_replace']=permission=='versions'
        employee=data.get('employee')
        if employee and employee.role=='subcontractor' and self.actor and not self.actor.is_leader and data.get('enabled'):
            from .access import eligible_subcontract_users
            if not isinstance(self.document,Document) or not eligible_subcontract_users(self.actor,self.document).filter(pk=employee.pk).exists():self.add_error('employee','Нет действующей связи представителя с этим документом субподрядчика. Можно только закрыть прежний доступ.')
        return data

class PersonForm(forms.ModelForm):
    def __init__(self,*args,actor=None,**kwargs):
        super().__init__(*args,**kwargs)
        from postoffice.forms import add_request_fields
        add_request_fields(self,actor)
    def clean(self):
        from postoffice.forms import validate_request
        return validate_request(self,super().clean())
    expected_revision=forms.IntegerField(widget=forms.HiddenInput,required=False)
    class Meta:
        model=Person
        fields=['full_name','position']

class AssignmentForm(forms.ModelForm):
    class Meta:
        model=PersonAssignment
        fields=['project_object','start_date','end_date']
        labels={'project_object':'Объект','start_date':'Начало работы','end_date':'Окончание работы'}
        widgets={key:forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}) for key in ['start_date','end_date']}
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs);self.fields['project_object'].queryset=visible_objects(user)
    def clean(self):
        data=super().clean()
        if data.get('start_date') and data.get('end_date') and data['end_date']<data['start_date']:self.add_error('end_date','Окончание раньше начала.')
        return data
