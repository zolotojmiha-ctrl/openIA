from django import template
from projects.diameters import label,display_text
register=template.Library()
register.filter('dn',label)
register.filter('dn_text',display_text)
