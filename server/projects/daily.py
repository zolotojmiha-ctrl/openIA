"""Daily quantities keep immutable revisions; customer changes never recalculate history."""
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
from datetime import date
from django import forms
from django.db import transaction
from django.db.models import Q
from django.core.exceptions import PermissionDenied,ValidationError
from accounts.permissions import permitted
from accounts.models import User
from accounts.views import audit
from .models import DailyWork,DailyWorkRevision,DailyWorkDecision,WORK_STAGES,Contract,ContractChangeRevision,BrigadeAssignment
from .access import visible_contracts,visible_objects
from .crews import visible_assignments

FIELDS=['date','stage','place','km_from','km_to','length','area','width','diameter','quantity','unit','downtime','reason','note','crew','change','abrasive']
def works(user):return DailyWork.objects.filter(contract__in=visible_contracts(user),project_object__in=visible_objects(user)).select_related('current_revision','active_revision','author','contract','project_object')
def prepare(user):return permitted(user,'works.prepare') or permitted(user,'projects.manage')
def crew_allowed(user,assignment,on):
    if prepare(user):return True
    return assignment.person_assignments.filter(person__user=user,person__archived=False,start_date__lte=on).filter(Q(end_date__isnull=True)|Q(end_date__gte=on)).exists()

class WorkForm(forms.Form):
    record=forms.UUIDField(widget=forms.HiddenInput)
    version=forms.IntegerField(widget=forms.HiddenInput,min_value=0)
    date=forms.DateField(label='Дата выполнения',widget=forms.DateInput(attrs={'type':'date'},format='%Y-%m-%d'))
    stage=forms.ChoiceField(label='Этап')
    place=forms.CharField(label='Секция / место работ',max_length=150)
    km_from=forms.CharField(label='Км от',required=False,max_length=40)
    km_to=forms.CharField(label='Км до',required=False,max_length=40)
    length=forms.DecimalField(label='Длина, м',required=False,max_digits=12,decimal_places=3,min_value=0)
    area=forms.DecimalField(label='Площадь, м²',required=False,max_digits=12,decimal_places=3,min_value=0)
    width=forms.IntegerField(label='Ширина зачистки / шлифовки, мм',required=False,min_value=1)
    diameter=forms.IntegerField(label='Диаметр трубы, мм',required=False,min_value=1)
    quantity=forms.DecimalField(label='Количество (для иных СМР)',required=False,max_digits=12,decimal_places=3,min_value=0)
    unit=forms.ChoiceField(label='Единица количества',required=False,choices=[('','—'),('шт','шт'),('м','м'),('м²','м²'),('компл','компл')])
    downtime=forms.DecimalField(label='Простой, часов',required=False,max_digits=7,decimal_places=2,min_value=0)
    reason=forms.CharField(label='Причина простоя',required=False,max_length=500)
    note=forms.CharField(label='Примечание / изменение заказчика',required=False,max_length=3000,widget=forms.Textarea(attrs={'rows':3}))
    abrasive=forms.DecimalField(label='Расход купершлака, кг (справочно)',required=False,max_digits=12,decimal_places=3,min_value=0)
    crew=forms.ModelChoiceField(label='Назначение бригады',queryset=BrigadeAssignment.objects.none(),required=False)
    change=forms.ModelChoiceField(label='Подтверждённое указание заказчика',queryset=ContractChangeRevision.objects.none(),required=False)
    def __init__(self,*args,user,contract,obj,kind,**kwargs):
        super().__init__(*args,**kwargs);self.fields['stage'].choices=WORK_STAGES.get(kind,[])
        self.fields['crew'].queryset=visible_assignments(user).filter(contract=contract,project_object=obj)
        self.fields['change'].queryset=ContractChangeRevision.objects.filter(change__contract=contract,decision__action='approved')
        self.fields['change'].label_from_instance=lambda r:f'{r.title} · редакция {r.number}'
    def clean(self):
        c=super().clean()
        for key in ('km_from','km_to'):
            if c.get(key):
                try:
                    value=Decimal(c[key].replace(',','.')).quantize(Decimal('.001'),rounding=ROUND_HALF_UP)
                    if not value.is_finite() or value<0 or value>999999:raise InvalidOperation
                    c[key]=str(value)
                except (InvalidOperation,ValueError):self.add_error(key,'Укажите неотрицательный километр.')
        if c.get('km_from') and c.get('km_to') and Decimal(c['km_from'])>Decimal(c['km_to']):self.add_error('km_to','Окончание раньше начала.')
        if c.get('stage')=='downtime':
            if not c.get('downtime') or not c.get('reason'):self.add_error('reason','Для простоя нужны продолжительность и причина.')
            if any(c.get(k) for k in ('length','area','quantity')):self.add_error(None,'Простой и выполненные объёмы записывайте отдельно.')
        elif not any(c.get(k) for k in ('length','area','quantity')):self.add_error(None,'Укажите выполненную длину, площадь или количество.')
        elif c.get('downtime'):self.add_error('downtime','Простой записывается отдельной строкой.')
        if c.get('abrasive') and not c.get('stage','').startswith('blast_'):self.add_error('abrasive','Купершлак указывается для пескоструйного этапа. Фактическое складское списание оформляется отдельно.')
        if c.get('quantity') and not c.get('unit'):self.add_error('unit','Выберите единицу количества.')
        if c.get('stage') in ('grind_long','grind_cross') and not c.get('width'):self.add_error('width','Укажите ширину для этой записи выполнения.')
        return c

@transaction.atomic
def submit(user,contract_id,object_id,kind,raw):
    actor=User.objects.select_for_update().get(pk=user.pk)
    if not actor.is_active or actor.session_version!=user.session_version:raise PermissionDenied
    contract=Contract.objects.select_for_update().get(pk=contract_id)
    obj=visible_objects(actor).filter(pk=object_id,contracts=contract).first()
    if not obj or obj.status=='archived' or not visible_contracts(actor).filter(pk=contract.pk).exists() or contract.status=='archived':raise PermissionDenied
    if kind not in contract.work_kinds or kind not in WORK_STAGES:raise ValidationError('Этот вид работ не выбран в контракте.')
    form=WorkForm(raw,user=actor,contract=contract,obj=obj,kind=kind)
    if not form.is_valid():raise ValidationError([str(v) for errors in form.errors.values() for v in errors])
    c=form.cleaned_data;assignment=c.get('crew')
    if assignment:
        assignment=BrigadeAssignment.objects.select_for_update().get(pk=assignment.pk)
        if c['date']<assignment.start_date or assignment.end_date and c['date']>assignment.end_date:raise ValidationError('Дата вне периода бригады.')
        if not crew_allowed(actor,assignment,c['date']):raise PermissionDenied
    elif not prepare(actor):raise ValidationError('Для внесения результата выберите своё назначение бригады.')
    if c.get('change'):
        ids=c['change'].object_ids
        if ids and str(obj.pk) not in ids:raise ValidationError('Указание относится к другим объектам.')
    def scalar(value):
        if value is None:return ''
        if isinstance(value,Decimal):return format(value.normalize(),'f')
        return str(getattr(value,'pk',value))
    data={k:scalar(c.get(k)) for k in FIELDS}
    row=DailyWork.objects.select_for_update().filter(pk=c['record']).first()
    if row:
        if row.contract_id!=contract.pk or row.project_object_id!=obj.pk or row.kind!=kind:raise PermissionDenied
        if not prepare(actor) and row.author_id!=actor.pk:raise PermissionDenied
        if row.version!=c['version']:raise ValidationError('Выполнение изменено. Обновите страницу.')
        if row.current_revision.data==data:return row
        if row.state=='confirmed' and not actor.is_leader:raise ValidationError('Подтверждённое выполнение исправляет руководитель с сохранением истории.')
        row.version+=1
    else:
        if c['version']!=0:raise ValidationError('Запись не найдена.')
        row=DailyWork(pk=c['record'],contract=contract,project_object=obj,kind=kind,author=actor);row.save()
    revision=DailyWorkRevision.objects.create(work=row,number=row.revisions.count()+1,data=data,author=actor)
    row.current_revision=revision;row.state='reported';row.save(update_fields=['current_revision','state','version'])
    audit(actor,'work_reported',work=str(row.pk),contract=str(contract.pk),object=str(obj.pk),revision=revision.number)
    return row

@transaction.atomic
def decide(user,pk,version,action,note=''):
    actor=User.objects.select_for_update().get(pk=user.pk)
    if not actor.is_active or not actor.is_leader or actor.session_version!=user.session_version:raise PermissionDenied
    original=works(actor).get(pk=pk)
    contract=Contract.objects.select_for_update().get(pk=original.contract_id)
    row=works(actor).select_for_update(of=('self',)).get(pk=pk)
    if contract.status=='archived' or row.version!=version or row.state!='reported':raise ValidationError('Запись изменена или уже проверена.')
    if action not in ('confirmed','revision') or action=='revision' and not note.strip() or len(note)>2000:raise ValidationError('При возврате укажите причину, до 2000 символов.')
    DailyWorkDecision.objects.create(revision=row.current_revision,actor=actor,action=action,note=note)
    row.state=action;row.version+=1
    if action=='confirmed':row.active_revision=row.current_revision
    row.save(update_fields=['state','version','active_revision'])
    audit(actor,'work_decided',work=str(row.pk),state=action)
    return row
