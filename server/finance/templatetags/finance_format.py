from decimal import Decimal
from django import template
register=template.Library()
@register.filter
def rub(value):
    if value is None:return 'Нет данных'
    return f'{Decimal(value):,.2f}'.replace(',',' ').replace('.',',')+' ₽'
@register.filter
def million(value):
    if value is None:return 'Нет данных'
    return f'{Decimal(value)/Decimal(1000000):.2f}'.replace('.',',')+' млн ₽'
