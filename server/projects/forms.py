from django import forms
from accounts.models import User
from .models import ProjectObject, Contract, SHARED_FIELDS, COMPANY_TAX_ID

class ObjectForm(forms.ModelForm):
    expected_version = forms.IntegerField(widget=forms.HiddenInput,required=False)
    class Meta:
        model=ProjectObject
        fields=['name','kind','location','status','start_date','end_date']
        widgets={key:forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}) for key in ['start_date','end_date']}

class ContractForm(forms.ModelForm):
    expected_version = forms.IntegerField(widget=forms.HiddenInput,required=False)
    linked_objects = forms.ModelMultipleChoiceField(label='Связанные объекты',queryset=ProjectObject.objects.none(),required=False,widget=forms.CheckboxSelectMultiple)
    class Meta:
        model=Contract
        fields=['number','date','subject','counterparty','deadline','amount','status','private_notes']
        widgets={key:forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}) for key in ['date','deadline']}
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['linked_objects'].queryset=ProjectObject.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID).order_by('name')
        self.fields['amount'].min_value=0

class EmployeeChoice(forms.ModelMultipleChoiceField):
    def label_from_instance(self,user):
        return f'{user.full_name} — {user.get_role_display() or "сотрудник"}'+(' (заблокирован)' if not user.is_active else '')

class AssignmentForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    employees=EmployeeChoice(label='Сотрудники, которым доступен объект',queryset=User.objects.none(),required=False,widget=forms.CheckboxSelectMultiple)
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['employees'].queryset=User.objects.filter(leader_slot__isnull=True).order_by('full_name')

class ContractShareForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    employee=forms.ModelChoiceField(label='Сотрудник',queryset=User.objects.none())
    enabled=forms.BooleanField(label='Разрешить просмотр ограниченной карточки',required=False)
    fields=forms.MultipleChoiceField(label='Какие сведения разрешить',choices=list(SHARED_FIELDS.items()),required=False,widget=forms.CheckboxSelectMultiple)
    def __init__(self,*args,contract,**kwargs):
        super().__init__(*args,**kwargs)
        employees=User.objects.filter(leader_slot__isnull=True,object_assignments__project_object__contracts=contract).distinct().order_by('full_name')
        self.fields['employee'].queryset=employees
        self.fields['employee'].label_from_instance=lambda u:u.full_name
