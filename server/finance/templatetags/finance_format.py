from decimal import Decimal
from django import template
register=template.Library()

@register.filter
def rub(value):
    if value is None:return 'Нет данных'
    amount=Decimal(str(value))/Decimal(1000)
    text=format(amount,'f')
    if '.' in text:text=text.rstrip('0').rstrip('.')
    whole,_,fraction=text.partition('.')
    whole=f'{int(whole):,}'.replace(',',' ')
    if amount<0 and int(amount)==0:whole='-0'
    return whole+(','+fraction if fraction else '')+' тыс. руб.'

@register.filter
def million(value):
    return rub(value)
