import re
from django import template

register=template.Library()


@register.filter
def compact_stock_name(item):
    name=item.name.strip()
    if name in ('','-','—') or re.match(r'^муфта\s+п(?:1|2|3|4|5у)\b',name,re.I):return ''
    return name
