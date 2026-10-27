from django import forms
from accounts.models import User
from .models import ProjectObject, Contract, SHARED_FIELDS, COMPANY_TAX_ID, WORK_KINDS
from .access import visible_objects,visible_contracts

class ObjectForm(forms.ModelForm):
    related_contract=forms.ModelChoiceField(label='Контракт заказчика',queryset=Contract.objects.none(),required=False,help_text='Участок будет добавлен в выбранный контракт.')
    def __init__(self,*args,user=None,**kwargs):
        super().__init__(*args,**kwargs)
        if not self.instance._state.adding:self.fields.pop('related_contract')
        elif user:
            self.fields['related_contract'].queryset=visible_contracts(user).exclude(status='archived')
            if self.initial.get('related_contract'):
                self.fields['related_contract'].disabled=True
                self.fields['related_contract'].required=True
        self.fields['name'].label='Название участка работ'
        self.fields['name'].help_text='Например: ОИ · Татарская — Омск · км 100–180. Границы выбираете вы.'
        self.fields['location'].label='Местоположение / НПС (необязательно)'
        self.fields['location'].required=False
    expected_version = forms.IntegerField(widget=forms.HiddenInput,required=False)
    class Meta:
        model=ProjectObject
        fields=['name','kind','location','status','start_date','end_date']
        widgets={key:forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}) for key in ['start_date','end_date']}

class ContractForm(forms.ModelForm):
    work_kinds=forms.MultipleChoiceField(label='Виды работ — можно выбрать несколько',choices=WORK_KINDS,widget=forms.CheckboxSelectMultiple)
    expected_version = forms.IntegerField(widget=forms.HiddenInput,required=False)
    linked_objects = forms.ModelMultipleChoiceField(label='Участки работ контракта',queryset=ProjectObject.objects.none(),required=False,widget=forms.CheckboxSelectMultiple(attrs={'data-bulk-check':'objects'}))
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
        self.fields['linked_objects'].required=False
        self.fields['linked_objects'].help_text='Сначала сохраните контракт, затем добавьте участки работ из его карточки.'
    def clean_linked_objects(self):
        places=self.cleaned_data['linked_objects']
        shared=places.filter(contracts__isnull=False)
        if not self.instance._state.adding:
            shared=shared.exclude(pk__in=self.instance.objects_linked.values('pk'))
        if shared.exists():
            raise forms.ValidationError('Участок уже относится к другому контракту. Создайте отдельный участок для этого контракта; работы и документы учитываются раздельно.')
        return places
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


def validate_new_area_links(contract,ids):
    """Serialize first attachment; historical shared links remain unchanged."""
    from .models import ContractObject
    list(ProjectObject.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
    previous=set(contract.objects_linked.values_list('pk',flat=True)) if contract and contract.pk else set()
    links=ContractObject.objects.filter(project_object_id__in=set(ids)-previous)
    if contract and contract.pk:links=links.exclude(contract=contract)
    if links.exists():raise forms.ValidationError('Участок уже добавлен в другой контракт. Создайте отдельный участок.')
