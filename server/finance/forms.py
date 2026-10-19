import uuid
from django import forms
from django.core.exceptions import ValidationError
from .models import FinancialScope,CashReceipt,PaymentAllocation,WorkExecution,AdvanceOffset,CostEntry,SubcontractObligation,PriceChange
from projects.models import Contract,ProjectObject,COMPANY_TAX_ID

class FinanceForm(forms.ModelForm):
    operation_token=forms.UUIDField(initial=uuid.uuid4,required=False,widget=forms.HiddenInput)
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        for name,field in self.fields.items():
            if isinstance(field,forms.DateField):field.widget=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'})
            if name in ('amount','paid','paid_amount','factual_amount','accepted_amount'):field.min_value=0
            if name=='scope':field.queryset=FinancialScope.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID)
            if name=='project_object':field.queryset=ProjectObject.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID)
            if name=='contract':field.queryset=Contract.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID)
    def clean(self):
        data=super().clean()
        for field in ['amount','paid','paid_amount','factual_amount','accepted_amount']:
            value=data.get(field)
            if value is not None and (value<0 or (field in ('amount','factual_amount') and value==0)):self.add_error(field,'Введите положительную сумму.')
        if data.get('paid') is not None and data.get('amount') is not None and data['paid']>data['amount']:self.add_error('paid','Оплата превышает обязательство.')
        if data.get('paid_amount') is not None and data.get('amount') is not None and data['paid_amount']>data['amount']:self.add_error('paid_amount','Оплата превышает затраты.')
        return data

class ScopeForm(FinanceForm):
    expected_revision=forms.IntegerField(widget=forms.HiddenInput,required=False)
    class Meta:model=FinancialScope;fields=['title','contract','vat_description','planned_profit']
class ReceiptForm(FinanceForm):
    confirm_duplicate=forms.BooleanField(label='Это отдельная выплата с совпадающими реквизитами',required=False)
    class Meta:model=CashReceipt;fields=['token','date','amount','source','reference'];widgets={'token':forms.HiddenInput}
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);self.fields['token'].initial=uuid.uuid4
    def clean(self):
        data=super().clean()
        if all(data.get(k) is not None for k in ['source','reference','date','amount']):
            duplicates=CashReceipt.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID,**{k:data[k] for k in ['source','reference','date','amount']})
            if self.instance.pk:duplicates=duplicates.exclude(pk=self.instance.pk)
            if duplicates.exists() and not data.get('confirm_duplicate'):self.add_error('confirm_duplicate','Выплата с такими реквизитами уже внесена. Проверьте, не повторяете ли вы запись.')
        return data

class WorkForm(FinanceForm):
    class Meta:model=WorkExecution;fields=['scope','title','date','factual_amount','source_pending'];widgets={'source_pending':forms.HiddenInput}
    def clean(self):
        data=super().clean();pending=data.get('source_pending')
        if pending and pending.scope_id!=getattr(data.get('scope'),'pk',None):self.add_error('scope','Строка справки относится к другому направлению.')
        return data

class AcceptanceForm(forms.Form):
    expected_revision=forms.IntegerField(widget=forms.HiddenInput)
    accepted_amount=forms.DecimalField(label='Принято по КС, ₽',max_digits=18,decimal_places=2,min_value=0.01)
    payment_due=forms.DateField(label='Срок оплаты по договору (можно уточнить позже)',required=False,widget=forms.DateInput(attrs={'type':'date'}))
    basis=forms.CharField(label='Основание принятия и срока оплаты',max_length=300)
class OffsetForm(FinanceForm):
    class Meta:model=AdvanceOffset;fields=['work','amount','date','basis']
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.fields['work'].queryset=WorkExecution.objects.filter(scope__legal_entity__tax_id=COMPANY_TAX_ID,accepted_by__isnull=False)
class AllocationForm(FinanceForm):
    class Meta:model=PaymentAllocation;fields=['scope','purpose','amount','work']
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.fields['work'].queryset=WorkExecution.objects.filter(scope__legal_entity__tax_id=COMPANY_TAX_ID,accepted_by__isnull=False)
    def clean(self):
        data=super().clean();work=data.get('work')
        if work and work.scope_id!=getattr(data.get('scope'),'pk',None):self.add_error('work','Выполнение относится к другому направлению.')
        if data.get('purpose')=='advance' and work:self.add_error('work','Аванс не является оплатой выполнения.')
        return data
class CostForm(FinanceForm):
    class Meta:model=CostEntry;fields=['scope','project_object','date','kind','amount','paid_amount','basis']
    def clean(self):
        data=super().clean();scope=data.get('scope');obj=data.get('project_object')
        if scope and scope.contract_id and obj and not scope.contract.objects_linked.filter(pk=obj.pk).exists():self.add_error('project_object','Объект не связан с выбранным договором.')
        return data

class SubcontractForm(FinanceForm):
    class Meta:model=SubcontractObligation;fields=['scope','counterparty','basis','amount','paid','due']
class PriceForm(FinanceForm):
    class Meta:model=PriceChange;fields=['contract','date','amount','basis']
    def clean(self):return forms.ModelForm.clean(self)
class ImportForm(forms.Form):
    file=forms.FileField(label='Справка XLSX')
    confirm=forms.BooleanField(label='Проверил показатели. Сохранить значения источника с отметками расхождений.',required=False)
