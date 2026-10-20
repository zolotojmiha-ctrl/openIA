from decimal import Decimal
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import render,redirect,get_object_or_404
from django.http import FileResponse,Http404
from django.utils import timezone
from accounts.views import leader_required,audit
from .access import visible_sections
from .models import Section,ImportBatch,Job,Defect,JobDefect,Coupling,Report,METHODS
from .importer import parse_work
from documents.storage import validate_upload,store_file,private_path,verified_file

class UploadForm(forms.Form):
    section=forms.ModelChoiceField(label='Участок, к которому относится ведомость',queryset=Section.objects.none())
    file=forms.FileField(label='Ведомость XLSX',widget=forms.FileInput(attrs={'accept':'.xlsx'}))
    sheet=forms.CharField(label='Название листа',max_length=150,required=False,help_text='Можно оставить пустым: будет выбран последний лист книги.')
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs);self.fields['section'].queryset=visible_sections(user)

def matches(section,item):
    query=Q(pk__in=[])
    for d in item['defects']:query|=Q(defects__number=d['number'])
    return Job.objects.filter(query,section=section).prefetch_related('defects').distinct()

class ReviewForm(forms.Form):
    include_couplings=forms.BooleanField(label='Сохранить также сведения о муфтах с заводскими номерами',required=False,initial=True)
    def __init__(self,*args,batch,**kwargs):
        super().__init__(*args,**kwargs)
        for index,item in enumerate(batch.payload['jobs']):
            candidates=list(matches(batch.section,item).filter(pk__in=item.get('candidates',[])))
            choices=[('new','Создать новую работу'),('skip','Пропустить')]
            choices += [(str(j.pk),'Добавить сведения в историю: '+', '.join(str(d) for d in j.defects.all())+' · '+j.get_state_display()) for j in candidates]
            self.fields[f'row_{index}']=forms.ChoiceField(label='Действие',choices=choices,initial='skip' if candidates else 'new')

@leader_required
def upload(request):
    form=UploadForm(request.POST or None,request.FILES or None,user=request.user)
    if request.method=='POST' and form.is_valid():
        try:
            payload=parse_work(form.cleaned_data['file'],form.cleaned_data['sheet'])
            for item in payload['jobs']:item['candidates']=[str(j.pk) for j in matches(form.cleaned_data['section'],item)]
            original=validate_upload(form.cleaned_data['file']);key=store_file(original)
            try:batch=ImportBatch.objects.create(section=form.cleaned_data['section'],author=request.user,filename=original['name'],storage_key=key,size=original['size'],sha256=payload['sha256'],sheet=payload['sheet'],payload=payload)
            except Exception:private_path(key).unlink(missing_ok=True);raise
            return redirect('repair_import_review',pk=batch.pk)
        except ValidationError as e:form.add_error('file',e.messages[0])
    return render(request,'repairs/import.html',{'form':form})

@leader_required
def source(request,pk):
    batch=get_object_or_404(ImportBatch.objects.filter(section__in=visible_sections(request.user)),pk=pk)
    if not batch.storage_key:raise Http404
    try:file=verified_file(batch)
    except (OSError,ValueError):raise Http404('Исходник недоступен.')
    response=FileResponse(file,as_attachment=True,filename=batch.filename,content_type='application/octet-stream')
    response['Cache-Control']='private, no-store';response['Content-Security-Policy']="default-src 'none'; sandbox";return response

@leader_required
def review(request,pk):
    batch=get_object_or_404(ImportBatch.objects.filter(section__in=visible_sections(request.user)),pk=pk)
    form=ReviewForm(request.POST or None,batch=batch)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            # Serialize all imports into one section, including repeats of the same file.
            Section.objects.select_for_update().get(pk=batch.section_id)
            locked=ImportBatch.objects.select_for_update().get(pk=batch.pk)
            if locked.applied_at or ImportBatch.objects.filter(section=batch.section,sha256=batch.sha256,sheet=batch.sheet,applied_at__isnull=False).exists():
                form.add_error(None,'Этот лист этой версии файла уже перенесён. Повторный импорт не выполняется.')
            else:
                new_candidates=[]
                for i,item in enumerate(batch.payload['jobs']):
                    action=form.cleaned_data[f'row_{i}']
                    if action=='new' and not list(matches(batch.section,item)):
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
                        source={'batch':str(batch.pk),'sha256':batch.sha256,'sheet':batch.sheet,'rows':item['rows'],'original':item['raw']}
                        if action=='new':
                            job=Job.objects.create(section=batch.section,created_by=request.user,method=item['method'],length_m=Decimal(item['length_m']) if item['length_m'] else None,composition=item['composition'],planned_date=item['planned_date'] or None,notes=item['note'],materials='missing' if 'нет муфт' in item['note'].lower() else 'unknown',source=source)
                            created+=1
                        else:
                            job=Job.objects.select_for_update().get(pk=action,section=batch.section);job.version+=1;job.save(update_fields=['version']);linked+=1
                            Report.objects.create(job=job,author=request.user,kind='imported',performed_date=timezone.localdate(),note=f'Сведения из ведомости «{batch.filename}», лист «{batch.sheet}», строки '+', '.join(map(str,item['rows']))+'. План и подтверждённое выполнение не изменены.',context={'source':source,'proposed_method':item['method'],'proposed_length':item['length_m'],'proposed_composition':item['composition']})
                        for d in item['defects']:
                            defect,_=Defect.objects.get_or_create(section=batch.section,kilometre=Decimal(d['km']),number=d['number'],defaults={'pipe_section':d['pipe_section'],'source':source})
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
                    return redirect('repairs')
    rows=[]
    for i,item in enumerate(batch.payload['jobs']):rows.append({'item':item,'method':dict(METHODS)[item['method']],'field':form[f'row_{i}']})
    return render(request,'repairs/import_review.html',{'batch':batch,'form':form,'rows':rows})
