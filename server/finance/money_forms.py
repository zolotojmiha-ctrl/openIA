"""UI amounts in thousands; domain services and stored values remain rubles."""
from decimal import Decimal,InvalidOperation
from django import forms

NAMES={'amount','paid','paid_amount','factual_amount','accepted_amount','planned_profit','work_price','idle_price','price','budget'}

def configure(form):
    names=[name for name,f in form.fields.items() if isinstance(f,forms.DecimalField) and (name in NAMES or '₽' in str(f.label or ''))]
    if not names:return
    form.fields['_money_unit']=forms.ChoiceField(choices=[('thousand','тыс. руб.'),('rubles','руб.')],initial='thousand',widget=forms.HiddenInput,required=False)
    # Preserve legacy/API POSTs and existing import formats that explicitly use rubles.
    if form.is_bound and form.data.get('_money_unit')!='thousand':return
    for name in names:
        field=form.fields[name];original=field.clean
        def clean(value,original=original):
            if value in (None,''):return original(value)
            try:value=Decimal(str(value).replace(' ','').replace(',','.'))*1000
            except (InvalidOperation,ValueError):raise forms.ValidationError('Введите сумму числом в тыс. руб.')
            if not value.is_finite():raise forms.ValidationError('Введите конечную сумму.')
            try:
                cents=value.quantize(Decimal('.01'))
            except InvalidOperation:raise forms.ValidationError('Слишком большая сумма.')
            if cents==value:value=cents
            return original(value)
        field.clean=clean
        value=form.initial.get(name,field.initial)
        if value is not None:
            try:form.initial[name]=format(Decimal(str(value))/1000,'f').rstrip('0').rstrip('.') if '.' in format(Decimal(str(value))/1000,'f') else format(Decimal(str(value))/1000,'f')
            except (InvalidOperation,ValueError):pass
        field.label=str(field.label or name).replace('₽','тыс. руб.')
        if 'тыс. руб.' not in field.label:field.label+=' (тыс. руб.)'
        field.widget.attrs.update(step='0.00001',inputmode='decimal')
        field.widget.attrs.pop('max',None)
        if field.min_value is not None:field.widget.attrs['min']=str(Decimal(str(field.min_value))/1000)

class MoneyForm(forms.Form):
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);configure(self)

class MoneyModelForm(forms.ModelForm):
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);configure(self)
