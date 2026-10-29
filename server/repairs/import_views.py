from decimal import Decimal
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import render,redirect,get_object_or_404
from django.http import FileResponse,Http404
from django.urls import reverse
from urllib.parse import urlencode
from projects.models import Contract
from projects.access import contract_card
from django.utils import timezone
from accounts.views import leader_required,audit
from accounts.models import User
from .access import can_prepare
from .access import visible_sections,prepare_required,repair_contracts,validate_contract_place,visible_batches
from .models import Section,ImportBatch,Job,Defect,JobDefect,Coupling,Report,METHODS
from .importer import parse_work
from .construction import planned_parts,COUPLING_METHODS
from .values import kilometre,matching_defects,get_or_create_defect
from documents.storage import validate_upload,store_file,private_path,verified_file

class UploadForm(forms.Form):
    contract=forms.ModelChoiceField(label='Контракт',queryset=Contract.objects.none())
    section=forms.ModelChoiceField(label='Участок, к которому относится ведомость',queryset=Section.objects.none())
    file=forms.FileField(label='Ведомость XLSX',widget=forms.FileInput(attrs={'accept':'.xlsx'}))
    sheet=forms.CharField(label='Название листа',max_length=150,required=False,help_text='Можно оставить пустым: будет выбран последний лист книги.')
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs);self.fields['section'].queryset=visible_sections(user)
        self.fields['contract'].queryset=repair_contracts(user)
        cid=self.data.get('contract') if self.is_bound else self.initial.get('contract')
        try:
            contract=self.fields['contract'].queryset.filter(pk=cid).first() if cid else None
        except (ValidationError,ValueError):contract=None
        if contract:self.fields['section'].queryset=self.fields['section'].queryset.filter(project_object__contracts=contract)
        self.fields['section'].label='Ведомость нефтепровода на участке'
        self.fields['contract'].label_from_instance=lambda c:contract_card(c,user)['title']
    def clean(self):
        c=super().clean()
        if c.get('section') and c.get('contract'):
            try:validate_contract_place(c['contract'],c['section'])
            except ValidationError as e:self.add_error('contract',e)
        return c

def matches(section,item,contract=None):
    query=Q(pk__in=[])
    for d in item['defects']:
        query|=Q(defects__number=d['number'],defects__kilometre__gte=max(Decimal(0),kilometre(d['km'])-Decimal('.0005')),defects__kilometre__lt=kilometre(d['km'])+Decimal('.0005'))
    return Job.objects.filter(query,section=section,contract=contract).prefetch_related('defects').distinct()

class ReviewForm(forms.Form):
    include_couplings=forms.BooleanField(label='Сохранить также сведения о муфтах с заводскими номерами',required=False,initial=True)
    def __init__(self,*args,batch,user,**kwargs):
        super().__init__(*args,**kwargs);self.batch=batch
        if self.is_bound:
            self.data=self.data.copy()
            for index,item in enumerate(batch.payload['jobs']):
                for name,key in ((f'length_{index}','length_m'),(f'composition_{index}','composition')):
                    if name not in self.data or self.data.get(f'row_{index}')!='new':self.data[name]=item.get(key,'')
        if not user.is_leader:
            self.fields['include_couplings'].initial=False
            self.fields['include_couplings'].disabled=True
        for index,item in enumerate(batch.payload['jobs']):
            candidates=list(matches(batch.section,item,batch.contract).filter(pk__in=item.get('candidates',[])))
            choices=[('new','Создать новую работу'),('skip','Пропустить')]
            choices += [(str(j.pk),'Добавить сведения в историю: '+', '.join(str(d) for d in j.defects.all())+' · '+j.get_state_display()) for j in candidates]
            self.fields[f'row_{index}']=forms.ChoiceField(label='Действие',choices=choices,initial='skip' if candidates else 'new')
            self.fields[f'length_{index}']=forms.DecimalField(label='Требуемая общая длина, м',required=False,min_value=Decimal('.001'),max_digits=9,decimal_places=3,initial=item.get('length_m') or None)
            self.fields[f'composition_{index}']=forms.CharField(label='Состав, м (если несколько муфт)',required=False,max_length=200,initial=item.get('composition',''))

    def clean(self):
        c=super().clean()
        from .views import assignees
        eligible=set(str(pk) for pk in assignees(self.batch.section,self.batch.contract).values_list('pk',flat=True))
        for index,item in enumerate(self.batch.payload['jobs']):
            if c.get(f'row_{index}')=='new' and item.get('assigned_to') and item['assigned_to'] not in eligible:
                self.add_error(None,'Доступ выбранного мастера изменился. Загрузите ведомость заново после проверки доступа.')
            selected=c.get(f'row_{index}')
            if selected=='new' and item['method'] in COUPLING_METHODS and f'length_{index}' not in self.errors and f'composition_{index}' not in self.errors:
                try:c[f'length_{index}'],_=planned_parts(c.get(f'length_{index}'),c.get(f'composition_{index}',''))
                except ValidationError as exc:self.add_error(f'composition_{index}' if c.get(f'composition_{index}') else f'length_{index}',exc)
            if selected not in ('skip',None) and any(matching_defects(self.batch.section,d['km'],d['number'],self.batch.contract).count()>1 for d in item['defects']):self.add_error(f'row_{index}','После округления есть дубли существующих дефектов. Проверьте их перед импортом.')
            if selected=='new' and any(matching_defects(self.batch.section,d['km'],d['number'],self.batch.contract).filter(archived_at__isnull=False).exists() for d in item['defects']):
                self.add_error(f'row_{index}','Этот дефект удалён из списка. Сначала восстановите его в разделе «Удалённые».')
            if self.batch.payload.get('format')=='openia-v1' and selected=='new':
                overlap=any(matching_defects(self.batch.section,d['km'],d['number'],self.batch.contract).filter(jobs__isnull=False).exists() for d in item['defects'])
                if overlap:
                    self.add_error(f'row_{index}','Дефект уже внесён. Пропустите строку или дополните историю существующей группы; повторный ремонт не создаётся автоматически.')
                if Job.objects.filter(section=self.batch.section,contract=self.batch.contract,source__repair_code=item['code']).exists():
                    self.add_error(f'row_{index}','Код ремонта уже используется на участке. Для нового ремонта укажите другой код.')
            if self.batch.payload.get('format')=='openia-v1' and selected not in ('new','skip',None):
                job=matches(self.batch.section,item,self.batch.contract).filter(pk=selected).first()
                desired={(kilometre(d['km']),d['number'],d['pipe_section']) for d in item['defects']}
                if not job or {(kilometre(d.kilometre),d.number,d.pipe_section) for d in job.defects.all()}!=desired:
                    self.add_error(f'row_{index}','Состав группы отличается. Импорт не объединяет и не заменяет дефекты существующего ремонта.')
        return c

@prepare_required
def upload(request):
    form=UploadForm(request.POST or None,request.FILES or None,user=request.user,initial={'contract':request.GET.get('contract'),'section':request.GET.get('section')})
    area=None
    back=reverse('repairs')
    if request.GET.get('contract') and request.GET.get('object'):
        from projects.access import visible_objects
        import uuid
        try:cid=uuid.UUID(request.GET['contract']);oid=uuid.UUID(request.GET['object'])
        except ValueError:raise Http404
        contract=get_object_or_404(repair_contracts(request.user),pk=cid)
        area=get_object_or_404(visible_objects(request.user).filter(contracts=contract).exclude(status='archived'),pk=oid)
        form.initial['contract']=contract.pk;form.fields['contract'].disabled=True
        form.fields['section'].queryset=visible_sections(request.user).filter(project_object=area)
        sections=form.fields['section'].queryset
        if sections.count()==1:
            form.initial['section']=sections.first().pk;form.fields['section'].disabled=True
        back=reverse('work_place',args=[contract.pk,area.pk])
    if request.method=='POST' and form.is_valid():
        try:
            payload=parse_work(form.cleaned_data['file'],form.cleaned_data['sheet'])
            if area:payload['from_area']=True
            section=form.cleaned_data['section']
            for item in payload['jobs']:
                if item.get('contract_id') and item['contract_id']!=str(form.cleaned_data['contract'].pk):raise ValidationError('В файле есть строки другого контракта. Выберите соответствующий контракт или подготовьте отдельную ведомость.')
                if item.get('section_id') and item['section_id']!=str(section.pk):
                    raise ValidationError('В файле есть строки другого участка. Перед экспортом выберите нужный участок или подготовьте отдельную ведомость.')
                item['candidates']=[str(j.pk) for j in matches(section,item,form.cleaned_data['contract'])]
                item['assigned_to']=None
                if item.get('master_name'):
                    from .views import assignees
                    masters=assignees(section,form.cleaned_data['contract']).filter(full_name__iexact=item['master_name'])
                    if masters.count()!=1:raise ValidationError('Мастер «'+item['master_name']+'»: укажите ФИО доступного мастера; совпадение должно быть единственным.')
                    item['assigned_to']=str(masters.get().pk)
                if item.get('master_phone'):
                    from .views import assignees
                    master=assignees(section,form.cleaned_data['contract']).filter(phone=item['master_phone']).first()
                    if not master:raise ValidationError('Мастер '+item['master_phone']+' не найден, неактивен или не имеет доступа к объекту. Проверьте доступ перед загрузкой.')
                    item['assigned_to']=str(master.pk);item['master_name']=master.full_name
            original=validate_upload(form.cleaned_data['file']);key=store_file(original)
            try:batch=ImportBatch.objects.create(contract=form.cleaned_data['contract'],section=form.cleaned_data['section'],author=request.user,filename=original['name'],storage_key=key,size=original['size'],sha256=payload['sha256'],sheet=payload['sheet'],payload=payload)
            except Exception:private_path(key).unlink(missing_ok=True);raise
            return redirect('repair_import_review',pk=batch.pk)
        except ValidationError as e:form.add_error('file',e.messages[0])
    return render(request,'repairs/import.html',{'form':form,'back':back,'area':area})

@prepare_required
def source(request,pk):
    batch=get_object_or_404(visible_batches(request.user),pk=pk)
    if not batch.contract_id and not request.user.is_leader:
        from .access import visible_jobs
        if Job.objects.filter(source__batch=str(batch.pk)).exclude(pk__in=visible_jobs(request.user,include_archived=True)).exists():raise Http404
    if not batch.storage_key:raise Http404
    try:file=verified_file(batch)
    except (OSError,ValueError):raise Http404('Исходник недоступен.')
    response=FileResponse(file,as_attachment=True,filename=batch.filename,content_type='application/octet-stream')
    response['Cache-Control']='private, no-store';response['Content-Security-Policy']="default-src 'none'; sandbox";return response

@prepare_required
def review(request,pk):
    batch=get_object_or_404(visible_batches(request.user),pk=pk)
    if batch.contract_id is None and not batch.applied_at:
        return render(request,'repairs/legacy_import.html',{'batch':batch},status=409)
    if batch.contract:
        try:validate_contract_place(batch.contract,batch.section)
        except ValidationError:raise Http404
    if not batch.contract_id and not request.user.is_leader:
        from .access import visible_jobs
        if Job.objects.filter(source__batch=str(batch.pk)).exclude(pk__in=visible_jobs(request.user,include_archived=True)).exists():raise Http404
    form=ReviewForm(request.POST or None,batch=batch,user=request.user)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            actor=User.objects.select_for_update().get(pk=request.user.pk)
            from projects.stock import lock as stock_lock
            stock_lock()
            if not can_prepare(actor):raise Http404
            if batch.contract_id:
                current_contract=get_object_or_404(Contract.objects.select_for_update().filter(pk__in=repair_contracts(actor).values('pk')),pk=batch.contract_id)
                try:validate_contract_place(current_contract,batch.section)
                except ValidationError:raise Http404
            # Serialize all imports into one section, including repeats of the same file.
            Section.objects.select_for_update().get(pk=batch.section_id)
            locked=ImportBatch.objects.select_for_update().get(pk=batch.pk)
            form=ReviewForm(request.POST,batch=batch,user=request.user)
            if not form.is_valid():pass
            elif locked.applied_at or ImportBatch.objects.filter(section=batch.section,contract=batch.contract,sha256=batch.sha256,sheet=batch.sheet,applied_at__isnull=False).exists():
                form.add_error(None,'Этот лист этой версии файла уже перенесён. Повторный импорт не выполняется.')
            else:
                new_candidates=[]
                for i,item in enumerate(batch.payload['jobs']):
                    action=form.cleaned_data[f'row_{i}']
                    if action=='new' and not list(matches(batch.section,item,batch.contract)):
                        # Empty at preview time must still be empty at commit time.
                        continue
                    if action=='new':
                        # Existing candidates are allowed only when they were explicitly visible in this review.
                        if form.fields[f'row_{i}'].initial!='skip':new_candidates.append(i)
                if new_candidates:form.add_error(None,'После предварительного просмотра появились совпадения. Обновите страницу и выберите действие для них.')
                else:
                    created=linked=0
                    for i,item in enumerate(batch.payload['jobs']):
                        action=form.cleaned_data[f'row_{i}']
                        if action=='skip':continue
                        source={'repair_code':item.get('code',''),'batch':str(batch.pk),'sha256':batch.sha256,'sheet':batch.sheet,'rows':item['rows'],'original':item['raw']}
                        if action=='new' and item['method'] in COUPLING_METHODS:
                            source['reviewed_plan']={'length_m':str(form.cleaned_data[f'length_{i}']),'composition':form.cleaned_data[f'composition_{i}'],'original_length':item['length_m'],'original_composition':item['composition']}
                        if action=='new':
                            job=Job.objects.create(contract=batch.contract,section=batch.section,created_by=request.user,method=item['method'],assigned_to_id=item.get('assigned_to'),wall_mm=Decimal(item['wall_mm']) if item.get('wall_mm') else None,length_m=form.cleaned_data[f'length_{i}'] if item['method'] in COUPLING_METHODS else (Decimal(item['length_m']) if item['length_m'] else None),composition=form.cleaned_data[f'composition_{i}'] if item['method'] in COUPLING_METHODS else item['composition'],planned_date=item['planned_date'] or None,notes=item['note'],materials='missing' if 'нет муфт' in item['note'].lower() else 'unknown',source=source)
                            created+=1
                        else:
                            job=Job.objects.select_for_update(no_key=True).get(pk=action,section=batch.section,contract=batch.contract);job.version+=1;job.save(update_fields=['version']);linked+=1
                            Report.objects.create(job=job,author=request.user,kind='imported',performed_date=timezone.localdate(),note=f'Сведения из ведомости «{batch.filename}», лист «{batch.sheet}», строки '+', '.join(map(str,item['rows']))+'. План и подтверждённое выполнение не изменены.',context={'source':source,'proposed_method':item['method'],'proposed_length':item['length_m'],'proposed_composition':item['composition']})
                        for d in item['defects']:
                            defect,_=get_or_create_defect(contract=batch.contract,section=batch.section,kilometre=Decimal(d['km']),number=d['number'],defaults={'pipe_section':d['pipe_section'],'source':source})
                            JobDefect.objects.get_or_create(job=job,defect=defect)
                    inventory=0
                    if form.cleaned_data['include_couplings']:
                        for c in batch.payload['couplings']:
                            observation={'sheet':batch.sheet,'row':c['row'],'sha256':batch.sha256,'historical_installation':c['historical_installation'],'mount_km':c.get('mount_km',''),'installation_date':c.get('installation_date',''),'original':c['raw']}
                            coupling,was_new=Coupling.objects.get_or_create(project_object=batch.section.project_object,method=c['method'],serial=c['serial'],defaults={'length_mm':c['length_mm'],'wall_mm':c['wall_mm'],'warehouse':c['warehouse'],'source':observation})
                            if not was_new:
                                coupling=Coupling.objects.select_for_update().get(pk=coupling.pk)
                                old=coupling.source.copy();observations=old.pop('observations',[])
                                coupling.source={**old,'observations':observations+[observation],'historical_installation':old.get('historical_installation',False) or c['historical_installation']}
                                coupling.save(update_fields=['source'])
                            inventory+=int(was_new)
                    locked.applied_at=timezone.now();locked.save(update_fields=['applied_at'])
                    audit(request.user,'repair_imported',entity_id=str(batch.pk),created=created,linked=linked,couplings=inventory)
                    messages.success(request,f'Создано работ: {created}. Дополнена история: {linked}. Добавлено муфт: {inventory}. Факт выполнения автоматически не подтверждался.')
                    if batch.payload.get('from_area') and batch.contract_id:return redirect('work_place',pk=batch.contract_id,object_id=batch.section.project_object_id)
                    return redirect('repairs')
    rows=[]
    for i,item in enumerate(batch.payload['jobs']):
        issue=''
        if item['method'] in COUPLING_METHODS:
            try:planned_parts(form[f'length_{i}'].value(),form[f'composition_{i}'].value())
            except ValidationError as exc:issue=exc.messages[0]
        rows.append({'item':item,'method':dict(METHODS)[item['method']],'field':form[f'row_{i}'],'length':form[f'length_{i}'],'composition':form[f'composition_{i}'],'coupling':item['method'] in COUPLING_METHODS,'plan_issue':issue})
    back=reverse('repair_import')
    if batch.payload.get('from_area') and batch.contract_id:back+='?'+urlencode({'contract':batch.contract_id,'object':batch.section.project_object_id})
    return render(request,'repairs/import_review.html',{'batch':batch,'form':form,'rows':rows,'back':back})
