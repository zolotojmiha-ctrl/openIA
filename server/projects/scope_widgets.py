"""Phone-friendly checklists shared by object and contract selectors."""
from django import forms

class ScopeChecklist(forms.CheckboxSelectMultiple):
    def render(self,name,value,attrs=None,renderer=None):
        from django.utils.html import format_html
        options=super().render(name,value,attrs,renderer)
        return format_html('<div class="scope-checklist" data-scope-checklist><input type="search" class="scope-search" placeholder="Найти в списке" aria-label="Поиск в списке"><div class="scope-options">{}</div><small class="muted" data-scope-count></small></div>',options)

class ScopedContracts(forms.ModelMultipleChoiceField):
    def __init__(self,*args,user=None,**kwargs):self.user=user;super().__init__(*args,**kwargs)
    def clean(self,value):
        if isinstance(value,str):value=[v.strip() for v in value.split(',') if v.strip()]
        return super().clean(value)
    def label_from_instance(self,obj):
        from .access import contract_card
        return contract_card(obj,self.user)['title'] if self.user else str(obj)


def values(params,key):
    raw=params.getlist(key) if hasattr(params,'getlist') else params.get(key,[])
    if not isinstance(raw,(list,tuple)):raw=[raw]
    return list(dict.fromkeys(v.strip() for entry in raw for v in str(entry).split(',') if v.strip()))


def identifiers(params,key):
    from uuid import UUID
    return [UUID(v) for v in values(params,key)]
