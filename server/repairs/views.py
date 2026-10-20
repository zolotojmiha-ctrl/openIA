import uuid
from decimal import Decimal, InvalidOperation
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError
from django.db.models import Q, Count
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.core import signing
from django import forms
from accounts.models import User
from accounts.views import leader_required, audit
from documents.storage import verified_file
from .access import visible_jobs, visible_sections, reportable_jobs, prepare_required, can_prepare
from .forms import SectionForm, JobForm, ReportForm, CouplingForm, AcceptanceForm
from .models import Section, Job, Defect, JobDefect, Coupling, Report, Attachment
from .services import report_work, confirm_work

@login_required
def jobs(request):
    qs=visible_jobs(request.user)
    sid=request.GET.get('section','');q=request.GET.get('q','').strip()[:150];state=request.GET.get('state','');year=request.GET.get('year','')
    try:
        if sid:qs=qs.filter(section_id=uuid.UUID(sid))
        if request.GET.get('object'):qs=qs.filter(section__project_object_id=uuid.UUID(request.GET['object']))
    except ValueError:qs=qs.none();sid=''
    if q:
        search=Q(defects__number__icontains=q)|Q(defects__pipe_section__icontains=q)|Q(section__name__icontains=q)|Q(section__pipeline__icontains=q)
        try:
            km=Decimal(q.replace(',','.'))
            if km.is_finite() and 0<=km<=999999:search|=Q(defects__kilometre=km)
        except InvalidOperation:pass
        qs=qs.filter(search).distinct()
    master=request.GET.get('master','')
    try:
        if master:qs=qs.filter(assigned_to_id=uuid.UUID(master))
    except ValueError:qs=qs.none()
    if year.isdigit() and 1900<=int(year)<=2200:qs=qs.filter(planned_date__year=int(year))
    counts={'total':qs.count(),'reported':qs.filter(state='reported').count(),'confirmed':qs.filter(state='confirmed').count(),'problems':qs.exclude(problem='').count()}
    if state=='problems':qs=qs.exclude(problem='')
    elif state in dict(Job._meta.get_field('state').choices):qs=qs.filter(state=state)
    elif state=='materials':qs=qs.filter(materials='missing')
    return render(request,'repairs/list.html',{'page':Paginator(qs.order_by('planned_date','created_at','pk'),25).get_page(request.GET.get('page')),'sections':visible_sections(request.user).order_by('rnu','pipeline','name'),'selected_section':sid,'q':q,'state':state,'year':year,'counts':counts,'can_prepare_repairs':can_prepare(request.user),'selected_master':master,'selected_object':request.GET.get('object',''),'masters':User.objects.filter(repair_jobs__in=visible_jobs(request.user)).distinct().order_by('full_name')})

@prepare_required
def section_new(request):
    form=SectionForm(request.POST or None,user=request.user)
    if request.method=='POST' and form.is_valid():
        section=form.save();audit(request.user,'repair_prepared',entity_id=str(section.pk),kind='section')
        return redirect('repair_new_section_job',section_id=section.pk)
    return render(request,'repairs/edit.html',{'form':form,'title':'Добавить участок','kind':'section'})

@prepare_required
def job_edit(request,pk=None,section_id=None):
    job=get_object_or_404(visible_jobs(request.user),pk=pk) if pk else None
    initial={'section':section_id or request.GET.get('section')}
    form=JobForm(request.POST or None,user=request.user,instance=job,initial=initial)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            locked=Job.objects.select_for_update().get(pk=job.pk) if job else None
            if locked and locked.version!=form.cleaned_data['expected_version']:form.add_error(None,'Работу уже изменили. Обновите страницу.')
            elif locked and locked.state=='confirmed':form.add_error(None,'Подтверждённая история сохранена. Для изменения результата используйте «Внести исправление».')
            else:
                saved=form.save(commit=False)
                if job and job.reports.exists():
                    previous={(d.number,d.kilometre,d.pipe_section) for d in job.defects.all()}
                    desired={(n,form.cleaned_data['kilometre'],form.cleaned_data['pipe_section']) for n in form.cleaned_data['defect_numbers']}
                    if previous!=desired:form.add_error(None,'После начала работ нельзя заменить привязанные дефекты. Создайте отдельное задание; прежняя история сохранится.')
                if not form.errors:
                    before={'method':locked.method,'length':str(locked.length_m or ''),'composition':locked.composition,'assigned_to':str(locked.assigned_to_id or ''),'date':str(locked.planned_date or '')} if locked else {}
                    saved.created_by=job.created_by if job else request.user;saved.version=(locked.version+1) if locked else 1;saved.save()
                    desired=[]
                    for number in form.cleaned_data['defect_numbers']:
                        defect,_=Defect.objects.get_or_create(section=saved.section,kilometre=form.cleaned_data['kilometre'],number=number,defaults={'pipe_section':form.cleaned_data['pipe_section']})
                        desired.append(defect)
                    JobDefect.objects.filter(job=saved).exclude(defect__in=desired).delete()
                    for defect in desired:JobDefect.objects.get_or_create(job=saved,defect=defect)
                    audit(request.user,'repair_prepared',entity_id=str(saved.pk),kind='job',previous=before)
                    messages.success(request,'Работа сохранена.');return redirect('repair_detail',pk=saved.pk)
    return render(request,'repairs/edit.html',{'form':form,'title':'Изменить задание' if job else 'Новая работа','kind':'job','job':job})

@login_required
def detail(request,pk):
    job=get_object_or_404(visible_jobs(request.user),pk=pk)
    reports=job.reports.select_related('author','confirmed_by').prefetch_related('attachments')
    result=reports.filter(kind__in=['partial','complete','correction']).first()
    history_page=Paginator(reports,20).get_page(request.GET.get('history_page'))
    return render(request,'repairs/detail.html',{'job':job,'reports':history_page,'history_page':history_page,'result':result,'couplings':job.installed_couplings.select_related('coupling'),'acceptance_form':AcceptanceForm(initial={'expected_version':job.version,'date':timezone.localdate()})})

@login_required
def report(request,pk,action):
    if action not in ('start','result','problem','note','correction','resolve','repaired','unrepaired'):raise Http404
    job=get_object_or_404(reportable_jobs(request.user),pk=pk)
    if action=='correction' and not request.user.is_leader:return render(request,'403.html',status=403)
    form=ReportForm(request.POST or None,request.FILES or None,job=job,action=action,user=request.user)
    status=200
    if request.method=='POST' and form.is_valid():
        try:
            result=report_work(request.user,job.pk,'unrepaired' if action=='unrepaired' else form.action,form.cleaned_data)
            messages.success(request,'Результат отправлен.' if action in ('result','correction') else 'Сведения сохранены.')
            if request.headers.get('Accept')=='application/json':
                from django.urls import reverse
                return JsonResponse({'ok':True,'url':reverse('repair_detail',args=[job.pk])})
            return redirect('repair_detail',pk=job.pk)
        except (ValidationError,Job.DoesNotExist) as e:
            if isinstance(e,ValidationError) and e.code=='already_saved' and request.headers.get('Accept')=='application/json':
                return JsonResponse({'ok':False,'errors':{'__all__':[{'message':e.messages[0]}]},'new_record_version':e.params['version']},status=409)
            form.add_error(None,e.messages[0] if isinstance(e,ValidationError) else 'Назначение изменилось. Откройте список своих работ.');status=409
    if request.method=='POST' and form.errors and request.headers.get('Accept')=='application/json':return JsonResponse({'ok':False,'errors':form.errors.get_json_data()},status=status if status!=200 else 400)
    titles={'repaired':'Устранён','unrepaired':'Не устранён','start':'Начать работу','result':'Внести результат','problem':'Сообщить о проблеме','note':'Добавить фото или пояснение','correction':'Исправить результат','resolve':'Препятствие устранено'}
    return render(request,'repairs/report.html',{'form':form,'job':job,'action':action,'title':titles[action]},status=status)

@leader_required
@require_POST
def confirm(request,pk):
    get_object_or_404(visible_jobs(request.user),pk=pk)
    try:confirm_work(request.user,pk,int(request.POST.get('expected_version','0')));messages.success(request,'Выполнение подтверждено. Приёмка заказчиком учитывается отдельно.')
    except (ValidationError,ValueError) as e:messages.error(request,e.messages[0] if isinstance(e,ValidationError) else 'Обновите карточку.')
    return redirect('repair_detail',pk=pk)

@leader_required
@require_POST
def accept(request,pk):
    job=get_object_or_404(visible_jobs(request.user),pk=pk)
    form=AcceptanceForm(request.POST)
    if form.is_valid():
        with transaction.atomic():
            job=Job.objects.select_for_update().get(pk=pk)
            latest=job.reports.filter(kind__in=['complete','partial','correction']).first()
            if job.version!=form.cleaned_data['expected_version'] or job.state!='confirmed' or job.accepted_date:messages.error(request,'Карточка изменилась или выполнение ещё не подтверждено.')
            elif latest and form.cleaned_data['date']<latest.performed_date:messages.error(request,'Приёмка не может быть раньше выполнения.')
            else:
                job.accepted_date=form.cleaned_data['date'];job.acceptance_basis=form.cleaned_data['basis'];job.accepted_by=request.user;job.version+=1;job.save()
                Report.objects.create(job=job,author=request.user,kind='acceptance',performed_date=job.accepted_date,note=job.acceptance_basis)
                audit(request.user,'repair_accepted',entity_id=str(job.pk),date=str(job.accepted_date),basis=job.acceptance_basis)
                messages.success(request,'Приёмка заказчиком отмечена. Финансовые записи автоматически не создаются.')
    else:messages.error(request,'Проверьте дату приёмки и укажите основание.')
    return redirect('repair_detail',pk=pk)

@login_required
def attachment(request,pk):
    item=get_object_or_404(Attachment.objects.filter(report__job__in=visible_jobs(request.user)),pk=pk)
    try:file=verified_file(item)
    except (OSError,ValueError):raise Http404('Файл недоступен.')
    inline=item.content_type in ('image/png','image/jpeg') and request.GET.get('preview')=='1'
    response=FileResponse(file,as_attachment=not inline,filename=item.name,content_type=item.content_type if inline else 'application/octet-stream')
    response['Cache-Control']='private, no-store';response['Content-Security-Policy']="default-src 'none'; sandbox";return response

@leader_required
def coupling_new(request,pk):
    section=get_object_or_404(visible_sections(request.user),pk=pk)
    form=CouplingForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        coupling=form.save(commit=False);coupling.project_object=section.project_object
        try:
            with transaction.atomic():coupling.full_clean();coupling.save();audit(request.user,'repair_prepared',entity_id=str(coupling.pk),kind='coupling')
            messages.success(request,'Муфта добавлена.');return redirect('repairs')
        except (ValidationError,IntegrityError):form.add_error(None,'Проверьте данные. Муфта с этим типом и заводским номером уже может быть добавлена.')
    return render(request,'repairs/edit.html',{'form':form,'title':'Добавить муфту','kind':'coupling'})

def assignees(section):
    return User.objects.filter(is_active=True).filter(Q(leader_slot__isnull=False)|Q(object_assignments__project_object=section.project_object)).distinct().order_by('full_name')

@prepare_required
def section_assignees(request,pk):
    section=get_object_or_404(visible_sections(request.user),pk=pk)
    return JsonResponse({'users':[{'id':str(u.pk),'name':u.full_name} for u in assignees(section)]})

class BatchAssignment(forms.Form):
    snapshot=forms.CharField(widget=forms.HiddenInput)
    master=forms.ModelChoiceField(label='Назначить мастера',queryset=User.objects.none())
    date=forms.DateField(label='Общая плановая дата — если нужна',required=False,widget=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}))
    jobs=forms.ModelMultipleChoiceField(label='Работы',queryset=Job.objects.none(),widget=forms.CheckboxSelectMultiple)
    def __init__(self,*args,section,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['master'].queryset=assignees(section);self.fields['master'].label_from_instance=lambda u:u.full_name
        base=Job.objects.filter(section=section,assigned_to__isnull=True).exclude(state='confirmed')
        ids=list(base.order_by('planned_date','pk').values_list('pk',flat=True)[:300])
        qs=base.filter(pk__in=ids).prefetch_related('defects').order_by('planned_date','pk')
        self.fields['jobs'].queryset=qs
        self.fields['jobs'].label_from_instance=lambda j:', '.join(str(d) for d in j.defects.all())+' · '+j.get_method_display()+' · '+(j.assigned_to.full_name if j.assigned_to_id else 'мастер не назначен')
        self.initial['snapshot']=signing.dumps({str(j.pk):j.version for j in qs},salt='repair-assignment')
    def clean_snapshot(self):
        try:return signing.loads(self.cleaned_data['snapshot'],salt='repair-assignment',max_age=8*60*60)
        except signing.BadSignature:raise forms.ValidationError('Список устарел. Обновите страницу.')
    def clean_jobs(self):
        qs=self.cleaned_data['jobs']
        if qs.count()>300:raise forms.ValidationError('За один раз можно назначить до 300 работ.')
        return qs

@prepare_required
def assign_many(request,pk):
    section=get_object_or_404(visible_sections(request.user),pk=pk)
    form=BatchAssignment(request.POST or None,section=section)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            selected=list(form.cleaned_data['jobs'].select_for_update().order_by('pk'))
            snapshot=form.cleaned_data['snapshot']
            if any(snapshot.get(str(j.pk))!=j.version or j.state=='confirmed' for j in selected):form.add_error(None,'Одна из работ изменилась. Назначения не сохранены; обновите список и проверьте результаты мастеров.')
            else:
                before=[{'id':str(j.pk),'master':str(j.assigned_to_id or ''),'date':str(j.planned_date or '')} for j in selected]
                for job in selected:
                    job.assigned_to=form.cleaned_data['master'];job.version+=1
                    if form.cleaned_data['date']:job.planned_date=form.cleaned_data['date']
                    job.save(update_fields=['assigned_to','planned_date','version'])
                audit(request.user,'repair_prepared',kind='assignment',section=str(section.pk),previous=before,master=str(form.cleaned_data['master'].pk))
                messages.success(request,f'Мастер назначен на {len(selected)} работ.');return redirect('repairs')
    return render(request,'repairs/assign.html',{'form':form,'section':section})
