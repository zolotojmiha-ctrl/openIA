import re
import uuid
from decimal import Decimal
from django import forms
from django.db.models import Q
from django.utils import timezone
from accounts.models import User
from projects.access import visible_objects,contract_card
from documents.storage import validate_upload
from .access import visible_sections, selectable_couplings,repair_contracts,validate_contract_place
from .values import kilometre,identifier,matching_defects
from .models import Section, Job, Defect, Coupling, METHODS, PROBLEMS

DATE=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'})

class SectionForm(forms.ModelForm):
    class Meta:
        model=Section
        fields=['project_object','rnu','pipeline','name','diameter_mm']
        labels={'project_object':'Объект'}
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['project_object'].queryset=visible_objects(user).exclude(status='archived')

class JobForm(forms.ModelForm):
    expected_version=forms.IntegerField(widget=forms.HiddenInput,initial=0)
    kilometre=forms.CharField(label='Километр',max_length=512,help_text='Округляется до трёх знаков после запятой.',widget=forms.TextInput(attrs={'inputmode':'decimal'}))
    defect_numbers=forms.CharField(label='Номер дефекта',max_length=1000,help_text='Если одна конструкция устраняет несколько дефектов, разделите номера знаком ;')
    pipe_section=forms.CharField(label='Номер секции трубы',max_length=80,required=False)
    class Meta:
        model=Job
        fields=['contract','section','method','length_m','wall_mm','composition','planned_date','assigned_to','materials','notes']
        labels={'section':'Участок','assigned_to':'Ответственный мастер'}
        widgets={'planned_date':DATE,'notes':forms.Textarea(attrs={'rows':3})}
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['contract'].queryset=repair_contracts(user)
        self.fields['contract'].label_from_instance=lambda c:contract_card(c,user)['title']
        self.fields['contract'].required=self.instance._state.adding or bool(self.instance.contract_id)
        self.fields['section'].queryset=visible_sections(user)
        sid=self.data.get('section') if self.is_bound else self.initial.get('section') or getattr(self.instance,'section_id',None)
        try:s=visible_sections(user).filter(pk=uuid.UUID(str(sid))).first()
        except (ValueError,TypeError):s=None
        self.fields['assigned_to'].queryset=User.objects.filter(is_active=True).filter(Q(leader_slot__isnull=False)|Q(object_assignments__project_object=s.project_object if s else None)).distinct().order_by('full_name') if s else User.objects.none()
        cid=self.data.get('contract') if self.is_bound else self.initial.get('contract') or getattr(self.instance,'contract_id',None)
        try:
            contract=self.fields['contract'].queryset.filter(pk=uuid.UUID(str(cid))).first()
        except (ValueError,TypeError):contract=None
        if contract:
            self.fields['section'].queryset=self.fields['section'].queryset.filter(project_object__in=contract.objects_linked.all())
            if s:self.fields['assigned_to'].queryset=self.fields['assigned_to'].queryset.filter(Q(leader_slot__isnull=False)|Q(contract_access__contract=contract)).distinct()
        if not self.instance._state.adding:
            self.fields['contract'].disabled=True
            self.initial['contract']=self.instance.contract_id
        self.fields['assigned_to'].label_from_instance=lambda u:u.full_name
        self.fields['length_m'].min_value=Decimal('.001')
        self.fields['wall_mm']=forms.DecimalField(label='Толщина стенки муфты, мм',max_digits=4,decimal_places=0,min_value=1,required=False,widget=forms.NumberInput(attrs={'step':1}),error_messages={'max_decimal_places':'Толщина стенки муфты — целое число, например 8, 10 или 12.'})
        if self.instance.pk and self.instance.section_id:
            if self.instance.wall_mm is not None and self.instance.wall_mm==self.instance.wall_mm.to_integral_value():self.initial['wall_mm']=int(self.instance.wall_mm)
            self.initial['section']=self.instance.section_id
            defects=sorted(self.instance.active_defects,key=lambda d:d.number)
            self.initial.update(expected_version=self.instance.version,defect_numbers='; '.join(d.number for d in defects),kilometre=defects[0].km if defects else None,pipe_section=defects[0].pipe_section if defects else '')
            self.fields['section'].disabled=True
    def clean_kilometre(self):return kilometre(self.cleaned_data['kilometre'])
    def clean_pipe_section(self):return identifier(self.cleaned_data.get('pipe_section',''),'№ секции',optional=True)
    def clean_wall_mm(self):
        value=self.cleaned_data.get('wall_mm')
        if value is not None and value<=0:
            raise forms.ValidationError('Толщина стенки должна быть больше нуля.')
        return value
    def clean_defect_numbers(self):
        values=[s.strip() for s in self.cleaned_data['defect_numbers'].split(';') if s.strip()]
        if not values or len(values)>30:raise forms.ValidationError('Укажите от 1 до 30 номеров дефектов.')
        values=[identifier(v,'№ дефекта') for v in values]
        return list(dict.fromkeys(values))
    def clean(self):
        c=super().clean()
        if c.get('contract') and c.get('section'):
            try:validate_contract_place(c['contract'],c['section'])
            except forms.ValidationError as e:self.add_error('contract',e)
        if c.get('method') in ['p1','p2','p3','p4','p5u'] and not c.get('length_m'):self.add_error('length_m','Укажите необходимую длину конструкции.')
        if c.get('section') and c.get('kilometre') is not None and c.get('defect_numbers'):
            if any(matching_defects(c['section'],c['kilometre'],number,c.get('contract')).filter(archived_at__isnull=False).exists() for number in c['defect_numbers']):
                self.add_error('defect_numbers','Этот дефект удалён из списка. Восстановите его в разделе «Удалённые», чтобы сохранить прежнюю историю.')
        if c.get('method') not in ('p1','p2','p3','p4','p5u') and c.get('wall_mm') is not None:self.add_error('wall_mm','Стенка муфты указывается только для ремонта муфтой.')
        if c.get('section') and c.get('kilometre') is not None and c.get('defect_numbers'):
            if any(matching_defects(c['section'],c['kilometre'],number,c.get('contract')).count()>1 for number in c['defect_numbers']):self.add_error('defect_numbers','После округления есть дубли существующих дефектов. Сначала проверьте их.')
        return c

class CouplingForm(forms.ModelForm):
    class Meta:
        model=Coupling
        fields=['method','serial','length_mm','wall_mm','warehouse']
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.fields['length_mm'].min_value=1;self.fields['wall_mm']=forms.DecimalField(label='Толщина стенки муфты, мм',max_digits=4,decimal_places=0,min_value=1,required=False,widget=forms.NumberInput(attrs={'step':1}),error_messages={'max_decimal_places':'Толщина стенки муфты — целое число, например 8, 10 или 12.'})

class MultipleInput(forms.ClearableFileInput):allow_multiple_selected=True
class MultipleFiles(forms.FileField):
    def clean(self,data,initial=None):
        if not data:return []
        items=data if isinstance(data,(list,tuple)) else [data]
        if len(items)>20:raise forms.ValidationError('За один раз можно добавить до 20 файлов.')
        if sum(item.size for item in items)>40*1024*1024:raise forms.ValidationError('За одну отправку можно добавить до 40 МБ файлов.')
        return [validate_upload(super(MultipleFiles,self).clean(item,initial)) for item in items]

class ReportForm(forms.Form):
    token=forms.UUIDField(widget=forms.HiddenInput)
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    performed_date=forms.DateField(label='Дата',widget=DATE)
    outcome=forms.ChoiceField(label='Результат',choices=[('partial','Выполнено частично'),('complete','Работа выполнена')],initial='complete',widget=forms.RadioSelect)
    method=forms.ChoiceField(label='Фактический метод ремонта',choices=METHODS,required=False)
    length_m=forms.DecimalField(label='Фактическая длина, м',max_digits=9,decimal_places=3,min_value=Decimal('.001'),required=False)
    composition=forms.CharField(label='Фактический состав, м',max_length=200,required=False)
    couplings=forms.ModelMultipleChoiceField(label='Все муфты, установленные на этой работе',queryset=Coupling.objects.none(),required=False,widget=forms.CheckboxSelectMultiple)
    problem=forms.ChoiceField(label='Почему дефект не устранён',choices=PROBLEMS,required=False)
    note=forms.CharField(label='Пояснение',max_length=3000,required=False,widget=forms.Textarea(attrs={'rows':3,'placeholder':'Можно надиктовать текст с клавиатуры телефона'}))
    files=MultipleFiles(label='Фотографии и документы',required=False,widget=MultipleInput())
    latitude=forms.DecimalField(max_digits=9,decimal_places=6,min_value=-90,max_value=90,required=False,widget=forms.HiddenInput)
    longitude=forms.DecimalField(max_digits=9,decimal_places=6,min_value=-180,max_value=180,required=False,widget=forms.HiddenInput)
    def __init__(self,*args,job,action,user,**kwargs):
        super().__init__(*args,**kwargs);self.action=action
        self.simple=action in ('repaired','unrepaired');self.job=job
        if self.simple:
            action='result' if action=='repaired' else 'problem'
            self.action=action
        self.initial.update(token=uuid.uuid4(),expected_version=job.version,performed_date=timezone.localdate(),method=job.method,length_m=job.length_m,composition=job.composition,couplings=list(job.installed_couplings.values_list('coupling_id',flat=True)))
        self.fields['couplings'].queryset=selectable_couplings(job,include_historical=user.is_leader)
        self.fields['couplings'].label_from_instance=lambda c:str(c)+(' · в ведомости указан монтаж'+(' на км '+c.source.get('mount_km','') if c.source.get('mount_km') else '')+(' от '+c.source.get('installation_date','') if c.source.get('installation_date') else '') if c.source.get('historical_installation') else '')
        if action not in ('result','correction'):
            for name in ['outcome','method','length_m','composition','couplings']:self.fields.pop(name)
        if action!='problem':self.fields.pop('problem')
        if action=='correction' and not user.is_leader:raise forms.ValidationError('Исправлять подтверждённый результат может руководитель.')
        if self.simple and action=='result':
            from projects.stock import prepared_couplings
            from django.core import signing
            self.prepared_stock_couplings=prepared_couplings(job)
            self.fields['stock_prepared']=forms.CharField(required=False,widget=forms.HiddenInput)
            self.initial['stock_prepared']=signing.dumps({'job':str(job.pk),'actor':str(user.pk),'ids':sorted(str(c.pk) for c in self.prepared_stock_couplings)},salt='stock-prepared')
        if self.simple:
            for name in ['outcome','method','length_m','composition','couplings']:
                self.fields.pop(name,None)
            if action=='problem':
                self.fields['problem'].choices=[('', 'Выберите причину'),('not_started','Ещё не выполняли'),('access','Нет проезда'),('winter','Нет проезда — доступ зимой'),('existing_coupling','Обнаружена ранее установленная муфта'),('oversize_p1','Нет муфты П1 увеличенного диаметра'),('swamp','Болото'),('coupling','Нет муфты'),('material_other','Нет другого материала'),('other','Другая причина')]
            if action in ('result','problem'):
                self.fields.pop('latitude');self.fields.pop('longitude')
                self.fields['coordinates']=forms.CharField(label='Координаты точки ремонта',max_length=100,required=action=='result',
                    widget=forms.TextInput(attrs={'placeholder':'56.123456, 73.123456','autocomplete':'off'}),
                    help_text='Скопируйте координаты точки из Google Карт и вставьте сюда. Например: 56.123456, 73.123456.')

    def clean_coordinates(self):
        text=self.cleaned_data['coordinates'].strip()
        if not text:return None
        # Google Maps: decimal degrees, latitude first. Also accept decimal commas
        # when the pair is separated unambiguously by a semicolon.
        pattern=r'([+-]?\d{1,3}(?:\.\d+)?)\s*[,;\s]\s*([+-]?\d{1,3}(?:\.\d+)?)'
        match=re.fullmatch(pattern,text)
        if not match and ';' in text:
            match=re.fullmatch(r'([+-]?\d{1,3}(?:,\d+)?)\s*;\s*([+-]?\d{1,3}(?:,\d+)?)',text)
        if not match:
            raise forms.ValidationError('Вставьте пару координат, например: 56.123456, 73.123456. Ссылка на карту не подходит.')
        lat,lon=(Decimal(v.replace(',','.')) for v in match.groups())
        if not -90<=lat<=90 or not -180<=lon<=180:
            raise forms.ValidationError('Координаты вне допустимого диапазона: широта от −90 до 90, долгота от −180 до 180.')
        return (lat.quantize(Decimal('.000001')),lon.quantize(Decimal('.000001')))

    def clean(self):
        c=super().clean()
        if self.simple and c.get('coordinates'):
            c['latitude'],c['longitude']=c['coordinates']
        if self.simple and self.action=='result':
            if len({kilometre(d.kilometre) for d in self.job.active_defects})>1:
                self.add_error(None,'В группе дефекты на разных километрах. Техник должен разделить задание на отдельные ремонты с отдельными координатами.')
            from projects.stock import prepared_couplings
            from django.core import signing
            issued=prepared_couplings(self.job)
            token=c.get('stock_prepared','')
            try:
                payload=signing.loads(token,salt='stock-prepared') if token else {'job':str(self.job.pk),'ids':[]}
                if payload['job']!=str(self.job.pk) or payload['ids']!=sorted(str(c.pk) for c in issued):raise ValueError
            except (signing.BadSignature,ValueError,KeyError,TypeError):self.add_error(None,'Состав выданных муфт изменился. Откройте актуальную форму и проверьте изделия.')
            prepared={coupling.pk:coupling for coupling in [*Coupling.objects.filter(installation__job=self.job),*issued]}
            c.update(outcome='complete',method=self.job.method,length_m=self.job.length_m,composition=self.job.composition,couplings=list(prepared.values()))
        if self.simple and self.action=='problem' and c.get('problem') in ('material_other','other','existing_coupling','oversize_p1') and not c.get('note','').strip():
            self.add_error('note',{'existing_coupling':'Опишите найденную муфту: тип, состояние и другие известные сведения.','oversize_p1':'Опишите обнаруженную муфту и требуемую П1 увеличенного диаметра; укажите размер, если известен.'}.get(c.get('problem'),'Поясните, какого материала не хватает или укажите другую причину.'))
        if c.get('performed_date') and c['performed_date']>timezone.localdate():self.add_error('performed_date','Фактическая дата не может быть в будущем.')
        if self.action=='problem' and not c.get('problem'):self.add_error('problem','Выберите причину.')
        if self.action in ('result','correction'):
            if not c.get('method'):self.add_error(None if self.simple else 'method','Выберите фактический способ.')
            if c.get('method') in ['p1','p2','p3','p4','p5u']:
                if not c.get('length_m'):self.add_error(None if self.simple else 'length_m','В задании не указана длина конструкции. Руководитель должен уточнить задание.' if self.simple else 'Укажите фактическую длину.')
                if not self.simple and not c.get('couplings') and not c.get('note'):self.add_error('note','Если заводские номера пока неизвестны, укажите это в пояснении.')
            elif c.get('couplings'):self.add_error(None if self.simple else 'couplings','Для этого способа муфты не используются.')
            elif c.get('method')=='grind':c['length_m']=None;c['composition']=''
        if self.action=='correction' and not c.get('note'):self.add_error('note','Укажите причину исправления. Прежний результат останется в истории.')
        if self.action=='note' and not c.get('note') and not c.get('files'):self.add_error('note','Добавьте фото или пояснение.')
        if (c.get('latitude') is None)!=(c.get('longitude') is None):self.add_error(None,'Нужны обе координаты.')
        return c

class AcceptanceForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    date=forms.DateField(label='Дата приёмки',widget=DATE)
    basis=forms.CharField(label='Основание: акт / документ',max_length=500)
    def clean_date(self):
        date=self.cleaned_data['date']
        if date>timezone.localdate():raise forms.ValidationError('Дата приёмки не может быть в будущем.')
        return date
