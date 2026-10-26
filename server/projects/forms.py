from django import forms
from accounts.models import User
from .models import ProjectObject, Contract, SHARED_FIELDS, COMPANY_TAX_ID, WORK_KINDS
from .access import visible_objects

class ObjectForm(forms.ModelForm):
    expected_version = forms.IntegerField(widget=forms.HiddenInput,required=False)
    class Meta:
        model=ProjectObject
        fields=['name','kind','location','status','start_date','end_date']
        widgets={key:forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}) for key in ['start_date','end_date']}

class ContractForm(forms.ModelForm):
    work_kinds=forms.MultipleChoiceField(label='Виды работ — можно выбрать несколько',choices=WORK_KINDS,widget=forms.CheckboxSelectMultiple)
    expected_version = forms.IntegerField(widget=forms.HiddenInput,required=False)
    linked_objects = forms.ModelMultipleChoiceField(label='Связанные объекты',queryset=ProjectObject.objects.none(),required=False,widget=forms.CheckboxSelectMultiple(attrs={'data-bulk-check':'objects'}))
    class Meta:
        model=Contract
        fields=['number','date','subject','work_kinds','counterparty','deadline','amount','status','private_notes']
        labels={'number':'Номер контракта заказчика','counterparty':'Заказчик','subject':'Предмет контракта'}
        widgets={key:forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}) for key in ['date','deadline']}
    def __init__(self,*args,user=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['linked_objects'].queryset=ProjectObject.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID).order_by('name')
        self.fields['amount'].min_value=0
        self.fields['status'].choices=[v for v in self.fields['status'].choices if v[0]!='archived']
        if self.instance.status=='archived':self.fields.pop('status')
        if user:
            self.fields['linked_objects'].queryset=visible_objects(user).order_by('name')
            if not user.is_leader:
                self.fields.pop('amount');self.fields.pop('private_notes')
                if not self.instance._state.adding:
                    rule=self.instance.access_rules.filter(user=user).first()
                    for flag,name in [('show_number','number'),('show_date','date'),('show_subject','subject'),('show_counterparty','counterparty'),('show_deadline','deadline')]:
                        if not rule or not getattr(rule,flag):self.fields.pop(name,None)
        self.fields['linked_objects'].required=self.instance._state.adding
    def clean_work_kinds(self):
        return list(dict.fromkeys(self.cleaned_data['work_kinds']))

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
    def __init__(self,*args,contract,actor=None,**kwargs):
        super().__init__(*args,**kwargs)
        employees=User.objects.filter(leader_slot__isnull=True,object_assignments__project_object__contracts=contract).distinct().order_by('full_name')
        if actor and not actor.is_leader:
            rule=contract.access_rules.filter(user=actor).first()
            self.fields['fields'].choices=[(k,v) for k,v in SHARED_FIELDS.items() if rule and getattr(rule,k)]
        self.fields['employee'].queryset=employees
        self.fields['employee'].label_from_instance=lambda u:u.full_name
