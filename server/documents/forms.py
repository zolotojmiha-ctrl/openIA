from django import forms
from accounts.models import User
from projects.access import visible_objects,visible_contracts,contract_card
from .access import scoped_documents,has_permission
from .models import Document, KINDS, SECTIONS
from .storage import validate_upload

class ContractChoice(forms.ModelMultipleChoiceField):
    def __init__(self,*args,user,**kwargs):self.user=user;super().__init__(*args,**kwargs)
    def label_from_instance(self,obj):return contract_card(obj,self.user)['title']

class UploadForm(forms.ModelForm):
    file=forms.FileField(label='Файл — до 10 МБ')
    linked_objects=forms.ModelMultipleChoiceField(label='Объекты',queryset=None,required=False,widget=forms.CheckboxSelectMultiple)
    linked_people=forms.ModelMultipleChoiceField(label='Сотрудник, к которому относится персональный документ',queryset=User.objects.none(),required=False,widget=forms.CheckboxSelectMultiple)
    comment=forms.CharField(label='Комментарий к версии',max_length=2000,required=False,widget=forms.Textarea)
    class Meta:
        model=Document
        fields=['title','number','date','section','kind']
        widgets={'date':forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'})}
    def __init__(self,*args,user,**kwargs):
        self.user=user
        super().__init__(*args,**kwargs)
        self.fields['linked_objects'].queryset=visible_objects(user).order_by('name')
        self.fields['linked_contracts']=ContractChoice(label='Договоры',queryset=visible_contracts(user).order_by('id'),user=user,required=False,widget=forms.CheckboxSelectMultiple)
        if user.is_leader:
            self.fields['linked_people'].queryset=User.objects.order_by('full_name')
            self.fields['linked_people'].label_from_instance=lambda u:u.full_name
        else:
            self.fields['section'].choices=[choice for choice in SECTIONS if choice[0]==user.role]
            self.fields['kind'].choices=[('work','Рабочий документ')]
            self.fields['linked_people'].widget=forms.HiddenInput()
    def clean_file(self):
        value=self.cleaned_data['file'];self.file_data=validate_upload(value);return value
    def clean(self):
        data=super().clean()
        objects=data.get('linked_objects')
        if not self.user.is_leader and not objects:self.add_error('linked_objects','Выберите хотя бы один назначенный объект.')
        if data.get('kind')=='personal' and not data.get('linked_people'):self.add_error('linked_people','Укажите сотрудника персонального документа.')
        for contract in data.get('linked_contracts',[]):
            if not contract.objects_linked.filter(pk__in=[obj.pk for obj in objects or []]).exists():
                self.add_error('linked_contracts','Каждый договор должен иметь хотя бы один из выбранных объектов.')
        return data

class VersionForm(forms.Form):
    expected_revision=forms.IntegerField(widget=forms.HiddenInput)
    file=forms.FileField(label='Новый файл — до 10 МБ')
    comment=forms.CharField(label='Что изменено',max_length=2000,widget=forms.Textarea)
    def clean_file(self):
        value=self.cleaned_data['file'];self.file_data=validate_upload(value);return value

class ApprovalForm(forms.Form):
    decision=forms.ChoiceField(label='Решение',choices=[('approved','Утвердить'),('revision','Отправить на доработку')],widget=forms.RadioSelect)
    reason=forms.CharField(label='Что доработать / комментарий',max_length=2000,required=False,widget=forms.Textarea)
    def clean(self):
        data=super().clean()
        if data.get('decision')=='revision' and not data.get('reason','').strip():self.add_error('reason','Укажите, что нужно доработать.')
        return data

class ShareForm(forms.Form):
    expected_revision=forms.IntegerField(widget=forms.HiddenInput)
    employee=forms.ModelChoiceField(label='Сотрудник',queryset=User.objects.none())
    enabled=forms.BooleanField(label='Разрешить просмотр карточки и сведений о текущей версии',required=False)
    can_download=forms.BooleanField(label='Разрешить скачивать текущий файл целиком',required=False)
    can_history=forms.BooleanField(label='Разрешить просмотр истории всех версий',required=False)
    def __init__(self,*args,document,**kwargs):
        super().__init__(*args,**kwargs)
        ids=[u.pk for u in User.objects.filter(is_active=True,leader_slot__isnull=True) if scoped_documents(u).filter(pk=document.pk).exists()]
        self.fields['employee'].queryset=User.objects.filter(pk__in=ids).order_by('full_name')
        self.fields['employee'].label_from_instance=lambda u:u.full_name
