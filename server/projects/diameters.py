"""Approved DN labels; persisted dimensions stay physical for norms and compatibility."""
import re
from django import forms
from django.core.exceptions import ValidationError

DN_TO_OD={200:219,250:273,300:325,350:377,400:426,500:530,600:630,700:720,800:820,900:920,1000:1020,1200:1220}
OD_TO_DN={v:k for k,v in DN_TO_OD.items()}

def label(value):
    if value in (None,''):return '—'
    try:diameter=int(value)
    except (ValueError,TypeError):return str(value)
    return f'DN {OD_TO_DN[diameter]}' if diameter in OD_TO_DN else f'{diameter} мм (уточнить DN)'

def physical(value):
    if value in (None,''):return None
    match=re.fullmatch(r'\s*DN\s*(\d+)\s*',str(value),re.I)
    if match:
        try:return DN_TO_OD[int(match[1])]
        except KeyError:raise ValidationError('Диаметр DN отсутствует в утверждённом справочнике.')
    legacy=re.fullmatch(r'(\d+) мм \(уточнить DN\)',str(value))
    if legacy:return int(legacy[1])
    try:return int(value)
    except (ValueError,TypeError):raise ValidationError('Выберите номинальный диаметр DN.')

def display_text(text):
    def change(match):
        n=int(match[1])
        return label(n) if n in OD_TO_DN else match[0]
    return re.sub(r'Ø\s*(\d+)(?:\s*мм)?',change,str(text or ''))

class DiameterField(forms.TypedChoiceField):
    def __init__(self,*args,**kwargs):
        kwargs.pop('min_value',None);kwargs.pop('max_value',None)
        kwargs.setdefault('label','Номинальный диаметр')
        kwargs['choices']=[('','—'),*((od,f'DN {dn}') for dn,od in DN_TO_OD.items())]
        kwargs['coerce']=int;kwargs['empty_value']=None
        super().__init__(*args,**kwargs)
