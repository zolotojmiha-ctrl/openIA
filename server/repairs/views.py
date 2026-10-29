import uuid
from decimal import Decimal, InvalidOperation
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError,PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError
from django.db.models import Q, Count, Min, F
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.core import signing
from django import forms
from accounts.models import User
from accounts.views import leader_required, audit
from documents.storage import verified_file
from .access import visible_jobs, visible_sections, reportable_jobs, prepare_required, can_prepare
from .forms import SectionForm, JobForm, ReportForm, CouplingForm, AcceptanceForm
from .models import Section, Job, Defect, JobDefect, Coupling, Report, Attachment
from .services import report_work, confirm_work
from .navigation import detail_url,report_return_url
from .values import kilometre,get_or_create_defect

def register_context(request,scope=None):
    archived=request.GET.get('archive')=='1' and can_prepare(request.user)
    from .filters import filtered_jobs,filter_values
    params=filter_values(request.GET)
    if scope:params.update(scope)
    qs=filtered_jobs(request.user,params,apply_state=False)
    sid=params['section'];q=params['q'];state=params['state'];year=params['year'];master=params['master']
    counts={'total':qs.count(),'reported':qs.filter(state='reported').count(),'confirmed':qs.filter(state='confirmed').count(),'problems':qs.exclude(problem='').exclude(state='unrepairable').count(),'unrepairable':qs.filter(state='unrepairable').count(),'revision':qs.exclude(review_note='').count()}
    qs=filtered_jobs(request.user,params)
    review_filter=signing.dumps({'actor':str(request.user.pk),'filters':params},salt='repair-review-filter',compress=True) if request.user.is_leader and not archived else ''
    ordered=qs.annotate(first_km=Min('defects__kilometre',filter=Q(defects__archived_at__isnull=not archived))).order_by('first_km','created_at','pk')
    page=Paginator(ordered,25).get_page(request.GET.get('page'))
    offset=max(0,page.start_index()-1)
    previous_ids=list(ordered.values_list('pk',flat=True)[:offset]) if offset else []
    number=Defect.objects.filter(jobs__pk__in=previous_ids,archived_at__isnull=not archived).count()
    reportable=set(reportable_jobs(request.user).filter(pk__in=[j.pk for j in page]).values_list('pk',flat=True))
    groups=[]
    for job in page:
        defects=job.archived_defects if archived else job.active_defects
        rows=[]
        for d in sorted(defects,key=lambda d:(d.kilometre,d.number,d.pipe_section)):
            number+=1;rows.append({'number':number,'defect':d})
        if rows:groups.append({'job':job,'rows':rows,'rowspan':len(rows),'can_report':job.pk in reportable})
    from .stock_assignment import summaries
    stock_summaries=summaries(request.user,[group['job'] for group in groups])
    problem_reports={}
    for report in Report.objects.filter(job_id__in=[g['job'].pk for g in groups],kind='problem').order_by('-created_at','-pk'):
        problem_reports.setdefault(report.job_id,report)
    for group in groups:
        group['stock']=stock_summaries[group['job'].pk]
        problem=problem_reports.get(group['job'].pk)
        group['problem_pending']=bool(group['job'].problem and problem and not problem.confirmed_at)
    from projects.access import visible_contracts
    try:
        from uuid import UUID
        contract_readonly=visible_contracts(request.user).filter(pk=UUID(params['contract']),status='archived').exists()
    except (ValueError,TypeError):contract_readonly=False
    section_choices=visible_sections(request.user)
    if params['contract']:
        try:section_choices=section_choices.filter(project_object__contracts=uuid.UUID(params['contract']))
        except ValueError:section_choices=section_choices.none()
    if params['object']:
        try:section_choices=section_choices.filter(project_object_id=uuid.UUID(params['object']))
        except ValueError:section_choices=section_choices.none()
    master_scope={k:v for k,v in params.items() if k in ('contract','object','section','legacy')}
    from projects.access import visible_objects,contract_card
    area_choices=visible_objects(request.user)
    if params['contract']:
        try:area_choices=area_choices.filter(contracts=uuid.UUID(params['contract']))
        except ValueError:area_choices=area_choices.none()
    contract_choices=[contract_card(c,request.user) for c in visible_contracts(request.user).order_by('number')]
    return {'scope_locked':bool(scope),'area_choices':area_choices,'contract_choices':contract_choices,'selected_legacy':params['legacy'],'return_to':request.get_full_path(),'contract_readonly':contract_readonly,'review_filter':review_filter,'page':page,'groups':groups,'archived':archived,'sections':section_choices.distinct().order_by('rnu','pipeline','name'),'selected_contract':params['contract'],'selected_section':sid,'q':q,'state':state,'year':year,'counts':counts,'can_prepare_repairs':can_prepare(request.user) and not contract_readonly,'selected_master':master,'selected_object':params['object'],'masters':User.objects.filter(repair_jobs__in=filtered_jobs(request.user,master_scope)).distinct().order_by('full_name')}

@login_required
def jobs(request):
    return render(request,'repairs/list.html',register_context(request))

class AreaDefectForm(forms.Form):
    pipeline=forms.CharField(label='Нефтепровод',max_length=200,help_text='Например: ОИ, УБО или ОП. Можно изменить для дефекта другого нефтепровода; километры разных нефтепроводов не смешиваются.')
    rnu=forms.CharField(label='РНУ контракта — заполните один раз',max_length=120,required=False,help_text='Сохранится в контракте и будет подставляться для следующих дефектов.')

@prepare_required
def area_defect(request,contract_id,object_id):
    from projects.access import visible_objects
    from .access import repair_contracts
    contract=get_object_or_404(repair_contracts(request.user),pk=contract_id)
    area=get_object_or_404(visible_objects(request.user).filter(contracts=contract).exclude(status='archived'),pk=object_id)
    existing_rnus=list(Section.objects.filter(project_object__contracts=contract).exclude(rnu='').values_list('rnu',flat=True).distinct())
    inherited_rnu=contract.rnu or (existing_rnus[0] if len(existing_rnus)==1 else '')
    pipelines=list(Section.objects.filter(project_object=area).values_list('pipeline',flat=True).distinct())
    header=AreaDefectForm(request.POST or None,initial={'rnu':inherited_rnu,'pipeline':pipelines[0] if len(pipelines)==1 else ''})
    if inherited_rnu:
        header.fields['rnu'].disabled=True
        header.fields['rnu'].widget=forms.HiddenInput()
    job_form=None
    if request.method=='POST' and header.is_valid():
        with transaction.atomic():
            from projects.models import Contract
            locked_contract=Contract.objects.select_for_update().get(pk=contract.pk)
            rnu=locked_contract.rnu or inherited_rnu or header.cleaned_data['rnu']
            if not locked_contract.rnu and rnu:
                locked_contract.rnu=rnu;locked_contract.version+=1;locked_contract.save(update_fields=['rnu','version'])
            # Technical Section is a ledger, not another user-created work area.
            section,_=Section.objects.get_or_create(project_object=area,rnu=rnu,pipeline=header.cleaned_data['pipeline'],name=area.name[:200])
            data=request.POST.copy();data['section']=str(section.pk);data['contract']=str(contract.pk);data['expected_version']='0'
            original=request.POST
            try:
                request.POST=data
                response=job_edit(request,section_id=section.pk)
            finally:request.POST=original
            if response.status_code==302:return response
            # Rebuild the same form while the temporary Section still exists.
            job_form=JobForm(data,user=request.user,initial={'contract':contract.pk,'section':section.pk})
            job_form.is_valid()
            if not job_form.errors:job_form.add_error(None,'Не удалось сохранить. Проверьте состояние контракта и повторите.')
            transaction.set_rollback(True)
    if job_form is None:
        job_form=JobForm(user=request.user,initial={'contract':contract.pk})
    if request.method!='POST':
        job_form.fields['assigned_to'].queryset=User.objects.filter(is_active=True).filter(Q(leader_slot__isnull=False)|Q(object_assignments__project_object=area,contract_access__contract=contract)).distinct().order_by('full_name')
    if request.user.is_leader:
        job_form.fields['assigned_to'].queryset=assignees(None,contract,request.user)
    job_form.fields['assigned_to'].help_text='Мастер должен иметь активный вход в ЛК с ролью «Мастер / прораб». Если доступа к участку ещё нет, отметьте флажок ниже. Чужие документы и финансы не открываются.'
    for name in ('contract','section','expected_version'):
        job_form.fields[name].widget=forms.HiddenInput()
    return render(request,'repairs/area_defect.html',{'header':header,'form':job_form,'card_id':contract.pk,'area':area,'inherited_rnu':inherited_rnu,'inherited_pipeline':pipelines[0] if len(pipelines)==1 else ''})

@prepare_required
def section_new(request):
    from projects.access import visible_objects
    from .access import repair_contracts
    initial={'project_object':request.GET.get('object')}
    contract=None;area=None
    if request.GET.get('contract') and request.GET.get('object'):
        contract=get_object_or_404(repair_contracts(request.user),pk=request.GET['contract'])
        area=get_object_or_404(visible_objects(request.user).filter(contracts=contract).exclude(status='archived'),pk=request.GET['object'])
        initial.update(project_object=area,name=area.name[:200],rnu=contract.rnu)
        if not initial['rnu']:
            rn=list(Section.objects.filter(project_object__contracts=contract).exclude(rnu='').values_list('rnu',flat=True).distinct())
            if len(rn)==1:initial['rnu']=rn[0]
    form=SectionForm(request.POST or None,user=request.user,initial=initial)
    if area:
        form.fields['project_object'].disabled=True
        if initial.get('rnu'):form.fields['rnu'].disabled=True
        form.fields['project_object'].label='Участок работ'
        form.fields['name'].label='Название ведомости'
        form.fields['name'].help_text='Можно оставить название участка. Здесь задаются нефтепровод и РНУ для загрузки дефектов.'
    if request.method=='POST' and form.is_valid():
        section=form.save();audit(request.user,'repair_prepared',entity_id=str(section.pk),kind='section')
        from django.urls import reverse
        from urllib.parse import urlencode
        if request.GET.get('next')=='import' and contract:
            return redirect(reverse('repair_import')+'?'+urlencode({'contract':contract.pk,'section':section.pk,'object':area.pk}))
        return redirect(reverse('repair_new_section_job',args=[section.pk])+'?'+urlencode({'contract':request.GET.get('contract','')}))
    return render(request,'repairs/edit.html',{'form':form,'title':'Подготовить ведомость нефтепровода' if area else 'Добавить ведомость нефтепровода','kind':'section'})

@prepare_required
def job_edit(request,pk=None,section_id=None):
    job=get_object_or_404(visible_jobs(request.user),pk=pk) if pk else None
    initial={'section':section_id or request.GET.get('section'),'contract':request.GET.get('contract')}
    form=JobForm(request.POST or None,user=request.user,instance=job,initial=initial)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            actor=User.objects.select_for_update().get(pk=request.user.pk)
            if not can_prepare(actor):raise PermissionDenied
            if job and not visible_jobs(actor).filter(pk=job.pk).exists():raise Http404
            target=form.cleaned_data.get('contract')
            if target:
                from projects.models import Contract
                from .access import repair_contracts,validate_contract_place
                target=get_object_or_404(Contract.objects.select_for_update().filter(pk__in=repair_contracts(actor).values('pk')),pk=target.pk)
                try:validate_contract_place(target,form.cleaned_data['section'])
                except ValidationError as e:
                    form.add_error('contract',e)
                    return render(request,'repairs/edit.html',{'form':form,'title':'Новая работа','kind':'job','job':job})
            Section.objects.select_for_update().get(pk=form.cleaned_data['section'].pk)
            locked=Job.objects.select_for_update(no_key=True).get(pk=job.pk) if job else None
            if locked and locked.version!=form.cleaned_data['expected_version']:form.add_error(None,'Работу уже изменили. Обновите страницу.')
            elif locked and locked.defects.filter(archived_at__isnull=False).exists():form.add_error(None,'В работе есть удалённые из списка дефекты. Сначала восстановите их; история и привязки сохраняются.')
            elif locked and locked.state=='confirmed':form.add_error(None,'Подтверждённая история сохранена. Для изменения результата используйте «Внести исправление».')
            else:
                saved=form.save(commit=False)
                if job and job.reports.exists():
                    previous={(d.number,kilometre(d.kilometre),d.pipe_section) for d in job.defects.all()}
                    desired={(n,form.cleaned_data['kilometre'],form.cleaned_data['pipe_section']) for n in form.cleaned_data['defect_numbers']}
                    if previous!=desired:form.add_error(None,'После начала работ нельзя заменить привязанные дефекты. Создайте отдельное задание; прежняя история сохранится.')
                if not form.errors:
                    before={'method':locked.method,'length':str(locked.length_m or ''),'composition':locked.composition,'assigned_to':str(locked.assigned_to_id or ''),'date':str(locked.planned_date or '')} if locked else {}
                    if locked and saved.assigned_to_id!=locked.assigned_to_id:saved.crew_assignment=None
                    if form.cleaned_data.get('open_assignment_access'):
                        from .assignment_access import grant
                        grant(actor,saved.assigned_to,saved.section,target)
                    saved.created_by=job.created_by if job else request.user;saved.version=(locked.version+1) if locked else 1;saved.save()
                    desired=[]
                    for number in form.cleaned_data['defect_numbers']:
                        defect,_=get_or_create_defect(section=saved.section,kilometre=form.cleaned_data['kilometre'],number=number,contract=saved.contract,defaults={'pipe_section':form.cleaned_data['pipe_section']})
                        desired.append(defect)
                    JobDefect.objects.filter(job=saved).exclude(defect__in=desired).delete()
                    for defect in desired:JobDefect.objects.get_or_create(job=saved,defect=defect)
                    audit(request.user,'repair_prepared',entity_id=str(saved.pk),kind='job',previous=before)
                    messages.success(request,'Работа сохранена.');return redirect('repair_detail',pk=saved.pk)
    return render(request,'repairs/edit.html',{'form':form,'title':'Изменить задание' if job else 'Новая работа','kind':'job','job':job})

@login_required
def detail(request,pk):
    job=get_object_or_404(visible_jobs(request.user,include_archived=can_prepare(request.user)),pk=pk)
    reports=job.reports.select_related('author','confirmed_by').prefetch_related('attachments')
    result=reports.filter(kind__in=['partial','complete','correction']).first()
    history_page=Paginator(reports,20).get_page(request.GET.get('history_page'))
    from projects.subcontracts import orders as subcontract_orders
    subcontract_open=bool(job.subcontract_order_id and subcontract_orders(request.user,True).filter(pk=job.subcontract_order_id).exists())
    from .navigation import return_url
    back=return_url(request,reverse('work_place',args=[job.contract_id,job.section.project_object_id]) if job.contract_id else reverse('repairs'))
    problem_report=job.reports.filter(kind='problem').first()
    return render(request,'repairs/detail.html',{'pending_problem':bool(job.problem and problem_report and not problem_report.confirmed_at),'back':back,'can_report_this_job':reportable_jobs(request.user).filter(pk=job.pk).exists(),'subcontract_open':subcontract_open,'job':job,'reports':history_page,'history_page':history_page,'result':result,'couplings':job.installed_couplings.select_related('coupling'),'acceptance_form':AcceptanceForm(initial={'expected_version':job.version,'date':timezone.localdate()})})

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
            messages.success(request,'Ремонт отправлен на проверку руководителю. Он отмечен зелёным в ведомости.' if form.action=='result' else 'Сведения сохранены.')
            if request.headers.get('Accept')=='application/json':
                from django.urls import reverse
                return JsonResponse({'ok':True,'url':report_return_url(request,job) if action in ('repaired','unrepaired','result') else detail_url(request,job.pk)})
            return redirect(report_return_url(request,job) if action in ('repaired','unrepaired','result') else detail_url(request,job.pk))
        except (ValidationError,Job.DoesNotExist) as e:
            if isinstance(e,ValidationError) and e.code=='already_saved' and request.headers.get('Accept')=='application/json':
                return JsonResponse({'ok':False,'errors':{'__all__':[{'message':e.messages[0]}]},'new_record_version':e.params['version']},status=409)
            form.add_error(None,e.messages[0] if isinstance(e,ValidationError) else 'Назначение изменилось. Откройте список своих работ.');status=409
    if request.method=='POST' and form.errors and request.headers.get('Accept')=='application/json':return JsonResponse({'ok':False,'errors':form.errors.get_json_data()},status=status if status!=200 else 400)
    titles={'repaired':'Устранён','unrepaired':'Не устранён','start':'Начать работу','result':'Внести результат','problem':'Сообщить о проблеме','note':'Добавить фото или пояснение','correction':'Исправить результат','resolve':'Препятствие устранено'}
    return render(request,'repairs/report.html',{'form':form,'job':job,'action':action,'title':titles[action],'report_back':report_return_url(request,job)},status=status)

@leader_required
@require_POST
def confirm(request,pk):
    get_object_or_404(visible_jobs(request.user),pk=pk)
    try:confirm_work(request.user,pk,int(request.POST.get('expected_version','0')));messages.success(request,'Выполнение подтверждено. Приёмка заказчиком учитывается отдельно.')
    except (ValidationError,ValueError) as e:messages.error(request,e.messages[0] if isinstance(e,ValidationError) else 'Обновите карточку.')
    return redirect(detail_url(request,pk))

@leader_required
@require_POST
def accept(request,pk):
    job=get_object_or_404(visible_jobs(request.user),pk=pk)
    form=AcceptanceForm(request.POST)
    if form.is_valid():
        with transaction.atomic():
            job=Job.objects.select_for_update(no_key=True).get(pk=pk)
            latest=job.reports.filter(kind__in=['complete','partial','correction']).first()
            if job.version!=form.cleaned_data['expected_version'] or job.state!='confirmed' or job.accepted_date:messages.error(request,'Карточка изменилась или выполнение ещё не подтверждено.')
            elif latest and form.cleaned_data['date']<latest.performed_date:messages.error(request,'Приёмка не может быть раньше выполнения.')
            else:
                job.accepted_date=form.cleaned_data['date'];job.acceptance_basis=form.cleaned_data['basis'];job.accepted_by=request.user;job.version+=1;job.save()
                Report.objects.create(job=job,author=request.user,kind='acceptance',performed_date=job.accepted_date,note=job.acceptance_basis)
                audit(request.user,'repair_accepted',entity_id=str(job.pk),date=str(job.accepted_date),basis=job.acceptance_basis)
                messages.success(request,'Приёмка заказчиком отмечена. Финансовые записи автоматически не создаются.')
    else:messages.error(request,'Проверьте дату приёмки и укажите основание.')
    return redirect(detail_url(request,pk))

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

def assignees(section,contract=None,actor=None):
    if actor and actor.is_leader:return User.objects.filter(is_active=True).filter(Q(role='foreman')|Q(leader_slot__isnull=False)).order_by('full_name')
    qs=User.objects.filter(is_active=True).filter(Q(leader_slot__isnull=False)|Q(object_assignments__project_object=section.project_object))
    if contract:qs=qs.filter(Q(leader_slot__isnull=False)|Q(contract_access__contract=contract))
    return qs.distinct().order_by('full_name')

@prepare_required
def section_assignees(request,pk):
    section=get_object_or_404(visible_sections(request.user),pk=pk)
    contract=None
    if request.GET.get('contract'):
        from .access import repair_contracts
        contract=get_object_or_404(repair_contracts(request.user),pk=request.GET['contract'])
        if not contract.objects_linked.filter(pk=section.project_object_id).exists():raise Http404
    return JsonResponse({'users':[{'id':str(u.pk),'name':u.full_name} for u in assignees(section,contract,request.user)]})

class BatchAssignment(forms.Form):
    snapshot=forms.CharField(widget=forms.HiddenInput)
    master=forms.ModelChoiceField(label='Назначить мастера',queryset=User.objects.none())
    date=forms.DateField(label='Общая плановая дата — если нужна',required=False,widget=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}))
    jobs=forms.ModelMultipleChoiceField(label='Работы',queryset=Job.objects.none(),widget=forms.CheckboxSelectMultiple)
    def __init__(self,*args,section,user,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['master'].queryset=assignees(section);self.fields['master'].label_from_instance=lambda u:u.full_name
        base=visible_jobs(user).filter(section=section,assigned_to__isnull=True,pk__in=Job.objects.filter(defects__archived_at__isnull=True).values('pk')).exclude(state='confirmed')
        ids=list(base.order_by('planned_date','pk').values_list('pk',flat=True)[:300])
        qs=base.filter(pk__in=ids).prefetch_related('defects').order_by('planned_date','pk')
        self.fields['jobs'].queryset=qs
        self.fields['jobs'].label_from_instance=lambda j:', '.join(str(d) for d in j.active_defects)+' · '+j.get_method_display()+' · '+(j.assigned_to.full_name if j.assigned_to_id else 'мастер не назначен')
        self.initial['snapshot']=signing.dumps({str(j.pk):j.version for j in qs},salt='repair-assignment')
    def clean_snapshot(self):
        try:return signing.loads(self.cleaned_data['snapshot'],salt='repair-assignment',max_age=8*60*60)
        except signing.BadSignature:raise forms.ValidationError('Список устарел. Обновите страницу.')
    def clean(self):
        c=super().clean()
        if c.get('master') and c.get('jobs'):
            for job in c['jobs']:
                if job.contract_id and not assignees(job.section,job.contract).filter(pk=c['master'].pk).exists():self.add_error('master','Мастер должен иметь доступ ко всем выбранным контрактам.')
        return c
    def clean_jobs(self):
        qs=self.cleaned_data['jobs']
        if qs.count()>300:raise forms.ValidationError('За один раз можно назначить до 300 работ.')
        return qs

@prepare_required
def assign_many(request,pk):
    section=get_object_or_404(visible_sections(request.user),pk=pk)
    form=BatchAssignment(request.POST or None,section=section,user=request.user)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            selected=list(form.cleaned_data['jobs'].select_for_update(no_key=True,of=('self',)).order_by('pk'))
            snapshot=form.cleaned_data['snapshot']
            if any(snapshot.get(str(j.pk))!=j.version or j.state=='confirmed' for j in selected):form.add_error(None,'Одна из работ изменилась. Назначения не сохранены; обновите список и проверьте результаты мастеров.')
            else:
                before=[{'id':str(j.pk),'master':str(j.assigned_to_id or ''),'date':str(j.planned_date or '')} for j in selected]
                for job in selected:
                    job.assigned_to=form.cleaned_data['master'];job.crew_assignment=None;job.version+=1
                    if form.cleaned_data['date']:job.planned_date=form.cleaned_data['date']
                    job.save(update_fields=['assigned_to','crew_assignment','planned_date','version'])
                audit(request.user,'repair_prepared',kind='assignment',section=str(section.pk),previous=before,master=str(form.cleaned_data['master'].pk))
                messages.success(request,f'Мастер назначен на {len(selected)} работ.');return redirect('repairs')
    return render(request,'repairs/assign.html',{'form':form,'section':section})


@prepare_required
def defect_visibility(request,pk,restore=False):
    """Reversible removal of one defect, preserving every report and attachment."""
    defect=get_object_or_404(Defect.objects.filter(section__in=visible_sections(request.user)),pk=pk)
    error='';status=200
    def snapshot(item,jobs):
        return {'defect':str(item.pk),'archived':str(item.archived_at or ''),'jobs':{str(j.pk):j.version for j in jobs}}
    from django.core.exceptions import PermissionDenied
    linked=Job.objects.filter(defects=defect)
    if linked.exclude(pk__in=visible_jobs(request.user,include_archived=True)).exists():raise PermissionDenied
    jobs=list(linked.order_by('pk'))
    if request.method=='POST':
        try:expected=signing.loads(request.POST.get('snapshot',''),salt='repair-defect-visibility',max_age=8*60*60)
        except signing.BadSignature:expected=None
        with transaction.atomic():
            jobs=list(Job.objects.select_for_update(no_key=True).filter(defects=defect).order_by('pk'))
            defect=Defect.objects.select_for_update().get(pk=defect.pk)
            if expected!=snapshot(defect,jobs):
                error='Запись изменилась. Проверьте данные и подтвердите действие заново.';status=409
            elif (defect.archived_at is None)==restore:
                error='Действие уже выполнено. Вернитесь к списку.';status=409
            else:
                defect.archived_at=None if restore else timezone.now()
                defect.archived_by=None if restore else request.user
                defect.save(update_fields=['archived_at','archived_by'])
                Job.objects.filter(pk__in=[j.pk for j in jobs]).update(version=F('version')+1)
                audit(request.user,'repair_defect_restored' if restore else 'repair_defect_archived',entity_id=str(defect.pk),jobs=[str(j.pk) for j in jobs])
                messages.success(request,'Дефект восстановлен.' if restore else 'Дефект удалён из списка. История и фотографии сохранены; запись можно восстановить.')
                return redirect('repairs')
    token=signing.dumps(snapshot(defect,jobs),salt='repair-defect-visibility')
    return render(request,'repairs/delete.html',{'defect':defect,'jobs':jobs,'restore':restore,'snapshot':token,'error':error},status=status)

class ReassignmentForm(forms.Form):
    open_assignment_access=forms.BooleanField(label='Открыть выбранному мастеру доступ к участку и заданиям контракта',required=False)
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    master=forms.ModelChoiceField(label='Новый ответственный мастер',queryset=User.objects.none(),required=False,empty_label='Пока не назначен')
    def __init__(self,*args,job,user=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.actor=user;self.job=job
        if not user or not user.is_leader:self.fields.pop('open_assignment_access',None)
        self.fields['master'].queryset=assignees(job.section,job.contract,user)
        if user and user.is_leader and job.assigned_to_id:
            self.fields['master'].queryset=(self.fields['master'].queryset|User.objects.filter(pk=job.assigned_to_id,is_active=True).exclude(role='customer')).distinct()
        self.fields['master'].label_from_instance=lambda user:user.full_name
        self.initial.update(expected_version=job.version,master=job.assigned_to_id)
    def clean(self):
        c=super().clean()
        from .assignment_access import has_access
        if c.get('master') and self.job.contract_id and not has_access(c['master'],self.job.section,self.job.contract) and not (self.actor and self.actor.is_leader and c.get('open_assignment_access')):
            self.add_error('master','У мастера нет доступа. Отметьте открытие доступа к участку или настройте его отдельно.')
        return c

@prepare_required
def reassign(request,pk):
    job=get_object_or_404(visible_jobs(request.user),pk=pk)
    form=ReassignmentForm(request.POST or None,job=job,user=request.user);status=200
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            from projects.operations import lock_actor
            actor=lock_actor(request.user,'repairs.prepare')
            current=Job.objects.select_for_update(no_key=True).get(pk=job.pk)
            if current.version!=form.cleaned_data['expected_version']:
                form.add_error(None,'Работа изменилась. Обновите страницу перед передачей.');status=409
            elif current.state=='confirmed':
                form.add_error(None,'Этот ремонт уже проверен и завершён. Передавайте новому мастеру оставшиеся неустранённые дефекты.');status=409
            else:
                master=form.cleaned_data['master']
                if form.cleaned_data.get('open_assignment_access'):
                    from .assignment_access import grant
                    grant(actor,master,current.section,current.contract)
                if current.assigned_to_id!=getattr(master,'pk',None):
                    before=current.assigned_to
                    Report.objects.create(job=current,author=request.user,kind='assignment',performed_date=timezone.localdate(),note='Ответственный мастер: '+(before.full_name if before else 'не назначен')+' → '+(master.full_name if master else 'не назначен')+'. Прежние результаты и авторы сохранены.',context={'from_master':str(current.assigned_to_id or ''),'to_master':str(getattr(master,'pk',''))})
                    current.assigned_to=master;current.crew_assignment=None;current.version+=1;current.save(update_fields=['assigned_to','crew_assignment','version'])
                    audit(request.user,'repair_prepared',entity_id=str(current.pk),kind='reassignment',previous_master=str(before.pk) if before else '',master=str(master.pk) if master else '')
                messages.success(request,'Ответственный мастер сохранён. Прежние записи остались в истории.')
                return redirect(detail_url(request,current.pk))
    return render(request,'repairs/reassign.html',{'job':job,'form':form},status=status)


@login_required
def local_drafts(request):
    # No draft contents leave this browser; opening a work still checks current ACL.
    response=render(request,'repairs/drafts.html')
    response['Cache-Control']='no-store'
    return response
