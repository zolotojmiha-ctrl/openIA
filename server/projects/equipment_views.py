from projects.scope_widgets import values
from uuid import UUID
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render,redirect,get_object_or_404
from django.utils import timezone
from django.urls import reverse
from accounts.permissions import permission_required,permitted
from documents.models import Document
from documents.access import visible_documents
from .models import Equipment,ProjectObject,Contract,EquipmentAssignment,EquipmentDocument,BrigadeAssignment
from .access import visible_objects,visible_contracts,contract_card
from . import equipment as service

DATE=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'})
class EquipmentForm(forms.ModelForm):
    expected_version=forms.IntegerField(widget=forms.HiddenInput,required=False)
    class Meta:
        model=Equipment
        fields=service.FIELDS
        widgets={'notes':forms.Textarea(attrs={'rows':3})}

class PlacementForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    project_object=forms.ModelChoiceField(label='Объект',queryset=ProjectObject.objects.none())
    contract=forms.ModelChoiceField(label='Контракт (при необходимости)',queryset=Contract.objects.none(),required=False)
    crew_assignment=forms.ModelChoiceField(label='Бригада на объекте (необязательно)',help_text='Даты работы техники независимы от дат бригады. Мастер отмечает наработку только за период своей ответственности.',queryset=BrigadeAssignment.objects.none(),required=False)
    start_date=forms.DateField(label='Начало работы',widget=DATE)
    end_date=forms.DateField(label='Окончание работы',widget=DATE,required=False)
    note=forms.CharField(label='Пояснение',max_length=1000,required=False,widget=forms.Textarea(attrs={'rows':2}))
    transfer=forms.BooleanField(label='Перевод: завершить прежнее открытое назначение накануне новой даты',required=False)
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['project_object'].queryset=visible_objects(user).exclude(status='archived')
        self.fields['contract'].queryset=visible_contracts(user).exclude(status='archived')
        from .crews import visible_assignments
        self.fields['crew_assignment'].queryset=visible_assignments(user).filter(Q(end_date__isnull=True)|Q(end_date__gte=timezone.localdate()))
        # Contract labels respect per-field grants.
        self.fields['contract'].label_from_instance=lambda c:contract_card(c,user)['title']

class EndForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    end_date=forms.DateField(label='Последний день работы',widget=DATE)

class StartForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    start_date=forms.DateField(label='Фактическое начало работы',widget=DATE)
    reason=forms.CharField(label='Причина исправления',max_length=500)

class FileForm(forms.ModelForm):
    expected_version=forms.IntegerField(widget=forms.HiddenInput,required=False)
    class Meta:
        model=EquipmentDocument
        fields=['document','label','expires_on','remind','days_before','active']
        labels={'document':'Файл документа','label':'Название документа на карточке техники','active':'Показывать связь с техникой'}
        widgets={'expires_on':DATE}
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs);self.fields['document'].queryset=visible_documents(user).filter(archived=False).order_by('title');self.fields['days_before'].min_value=0;self.fields['days_before'].max_value=365
        if self.instance.pk and not self.instance._state.adding:self.fields['document'].disabled=True

def filtered(user,params):
    qs=service.visible_equipment(user).order_by('name','inventory_number','pk')
    if params.get('archive')=='1' or params.get('state')=='archived':qs=qs.filter(status='archived')
    else:qs=qs.exclude(status='archived')
    q=str(params.get('q','')).strip()[:150]
    if q:qs=qs.filter(Q(name__icontains=q)|Q(inventory_number__icontains=q)|Q(registration_number__icontains=q)|Q(category__icontains=q))
    if params.get('ownership') in ('own','rent','subcontractor'):qs=qs.filter(ownership=params['ownership'])
    if params.get('state') in dict(Equipment._meta.get_field('status').choices):qs=qs.filter(status=params['state'])
    if params.get('object'):
        from uuid import UUID
        try:
            from .scope_widgets import identifiers
            ids=identifiers(params,'object')
        except ValueError:return qs.none()
        qs=qs.filter(pk__in=service.current(service.placements(user)).filter(project_object_id__in=ids).values('equipment_id'))
    return qs

@login_required
def listing(request):
    if request.user.role=='lessor' and not request.user.is_leader:return redirect('lessor_home')
    try:target_id=UUID(request.GET.get('assign_object',''))
    except ValueError:target_id=None
    target=visible_objects(request.user).filter(pk=target_id).first() if target_id else None
    from .crews import visible_assignments
    try:crew_id=UUID(request.GET.get('assign_crew',''))
    except ValueError:crew_id=None
    target_crew=visible_assignments(request.user).filter(pk=crew_id).first() if crew_id else None
    if target_crew:target=target_crew.project_object
    if target and permitted(request.user,'equipment.manage'):
        choices=visible_contracts(request.user).filter(objects_linked=target).exclude(status='archived')
        if target_crew:choices=choices.filter(pk=target_crew.contract_id)
        if choices.count()==1:
            url=reverse('area_equipment_pick',args=[choices.first().pk,target.pk])
            if target_crew:url+='?crew='+str(target_crew.pk)
            return redirect(url)
    qs=filtered(request.user,request.GET);page=Paginator(qs,25).get_page(request.GET.get('page'))
    location_map={}
    object_ids=set(visible_objects(request.user).values_list('pk',flat=True))
    for row in service.current(service.operational_placements(request.user)).filter(equipment__in=page.object_list):
        row.object_access=row.project_object_id in object_ids
        location_map.setdefault(row.equipment_id,[]).append(row)
    from . import rentals
    rental_map={}
    for r in rentals.visible(request.user).filter(placement__equipment__in=page.object_list,placement__start_date__lte=timezone.localdate()).filter(Q(placement__end_date__isnull=True)|Q(placement__end_date__gte=timezone.localdate())):rental_map.setdefault(r.placement.equipment_id,[]).append(r)
    rows=[{'item':item,'locations':location_map.get(item.pk,[]),'has_rental':item.pk in rental_map} for item in page.object_list]
    query=request.GET.copy();query.pop('page',None)
    return render(request,'projects/equipment_list.html',{'page':page,'rows':rows,'filter_query':query.urlencode(),'objects':visible_objects(request.user),'q':request.GET.get('q',''),'selected_object':request.GET.get('object',''),'selected_objects':values(request.GET,'object'),'state':request.GET.get('state',''),'target_object':target,'target_crew':target_crew,'archive':request.GET.get('archive')=='1' or request.GET.get('state')=='archived','ownership':request.GET.get('ownership',''),'ownerships':Equipment._meta.get_field('ownership').choices,'states':Equipment._meta.get_field('status').choices,'due':service.due_documents(request.user)})

@login_required
def detail(request,pk):
    item=get_object_or_404(service.visible_equipment(request.user),pk=pk)
    from urllib.parse import urlsplit
    back=request.GET.get('return_to','')
    try:parsed=urlsplit(back)
    except ValueError:back=''
    else:
        if len(back)>4096 or '\\' in back or parsed.scheme or parsed.netloc or parsed.path!=reverse('equipment'):back=''
    from . import equipment_issues
    from django.core.exceptions import PermissionDenied
    import uuid
    issue_error=''
    if request.method=='POST' and request.POST.get('action') in ('equipment_issue','resolve_issue'):
        try:
            if request.POST['action']=='equipment_issue':
                equipment_issues.submit(request.user,item.pk,request.POST.get('placement'),request.POST.get('description',''),uuid.UUID(request.POST.get('token','')))
            else:
                issue=get_object_or_404(equipment_issues.visible(request.user),pk=request.POST.get('issue'),equipment=item)
                equipment_issues.resolve(request.user,issue.pk,request.POST.get('resolution',''))
        except (ValidationError,ValueError):issue_error='Проверьте назначение техники и описание заявки.'
        else:
            messages.success(request,'Заявка сохранена.');return redirect('equipment_detail',pk=item.pk)
    issue_panel={'equipment_issues':equipment_issues.visible(request.user).filter(equipment=item).order_by('-created_at')[:30],
        'issue_placements':service.current(service.placements(request.user)).filter(equipment=item,contract__in=visible_contracts(request.user)),
        'issue_token':uuid.uuid4(),'issue_error':issue_error}
    cards={c.pk:contract_card(c,request.user) for c in visible_contracts(request.user)}
    from .crews import visible_assignments
    crews={a.pk:a.brigade.name for a in visible_assignments(request.user)}
    editable=set(service.placements(request.user).filter(equipment=item).values_list('pk',flat=True))
    object_ids=set(visible_objects(request.user).values_list('pk',flat=True))
    history=[{'row':a,'contract':cards.get(a.contract_id),'crew':crews.get(a.crew_assignment_id),
              'object_access':a.project_object_id in object_ids,'can_edit':a.pk in editable}
             for a in service.operational_placements(request.user).filter(equipment=item).order_by('-start_date','-pk')]
    files=list(service.equipment_files(request.user).filter(equipment=item))
    for link in files:
        days=(link.expires_on-timezone.localdate()).days if link.expires_on else None
        link.deadline_state='expired' if days is not None and days<0 else 'soon' if days is not None and days<=link.days_before else 'valid' if days is not None else 'none'
        link.deadline_label={'expired':'Просрочен','soon':'Скоро истекает','valid':'Действует','none':'Без срока'}[link.deadline_state]
    from . import rentals,rental_views
    available=rentals.visible(request.user).filter(placement__equipment=item).order_by('-placement__start_date','pk')
    today=timezone.localdate()
    active=available.filter(placement__start_date__lte=today).filter(Q(placement__end_date__isnull=True)|Q(placement__end_date__gte=today)).first()
    if request.method=='GET' and request.GET.get('rental'):
        from django.http import Http404
        try:rental_id=UUID(request.GET['rental'])
        except (ValueError,TypeError):raise Http404
        active=get_object_or_404(available,pk=rental_id)
    panel={}
    if request.method=='POST':
        from django.http import Http404
        try:rental_id=UUID(request.POST.get('rental_id',''))
        except (ValueError,TypeError):raise Http404
        active=get_object_or_404(available,pk=rental_id)
    if active:
        panel=rental_views.detail(request,active.pk,embedded=True)
        from django.http.response import HttpResponseBase
        if isinstance(panel,HttpResponseBase):return panel
    from .equipment_parts import search_links
    maintenance=[]
    for part in item.maintenance_parts.all():
        part.search=search_links(part);maintenance.append(part)
    return render(request,'projects/equipment_detail.html',{**panel,**issue_panel,'maintenance_parts':[p for p in maintenance if not p.archived],'maintenance_archived_parts':[p for p in maintenance if p.archived],'equipment_back':back or reverse('equipment'),'rental_history':available,'rental_unassigned':item.ownership=='rent' and not item.placements.exists(),'rental_restricted':item.ownership=='rent' and item.placements.exists() and not available.exists(),'custodies':item.custodies.select_related('brigade','driver').order_by('-start_date'),'item':item,'history':history,'files':files,'events':[]})

@permission_required('equipment.manage')
def edit(request,pk=None):
    item=get_object_or_404(service.visible_equipment(request.user),pk=pk) if pk else None
    form=EquipmentForm(request.POST or None,instance=item,initial={'expected_version':item.version} if item else None)
    status=200
    if request.method=='POST' and form.is_valid():
        try:saved=service.save_equipment(request.user,pk,form.cleaned_data.get('expected_version'),form.cleaned_data)
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:messages.success(request,'Карточка техники сохранена.');return redirect('equipment_detail',pk=saved.pk)
    return render(request,'projects/equipment_form.html',{'form':form,'item':item,'title':'Изменить технику' if item else 'Добавить технику'},status=status)

@permission_required('equipment.manage')
def assign(request,pk):
    item=get_object_or_404(service.visible_equipment(request.user),pk=pk)
    initial={'expected_version':item.version,'start_date':timezone.localdate()}
    if not request.GET.get('object') and not request.GET.get('crew'):
        from .crews import visible_assignments
        crews=list(service.current(visible_assignments(request.user)).filter(brigade_id__in=service.current(item.custodies.all()).values('brigade_id'))[:2])
        if len(crews)==1:initial.update(crew_assignment=crews[0].pk,project_object=crews[0].project_object_id,contract=crews[0].contract_id)
    if request.GET.get('object'):initial['project_object']=request.GET['object']
    if request.GET.get('crew'):
        from .crews import visible_assignments
        try:crew_id=UUID(request.GET['crew'])
        except ValueError:crew_id=None
        assignment=get_object_or_404(visible_assignments(request.user),pk=crew_id)
        initial.update(crew_assignment=assignment.pk,project_object=assignment.project_object_id,contract=assignment.contract_id,start_date=max(timezone.localdate(),assignment.start_date),end_date=assignment.end_date)
    form=PlacementForm(request.POST or None,user=request.user,initial=initial);status=200
    if request.method=='POST' and form.is_valid():
        c=form.cleaned_data
        try:service.assign(request.user,pk,c['expected_version'],c['project_object'],c['contract'],c['start_date'],c['end_date'],c['note'],c['transfer'],c['crew_assignment'])
        except ValidationError as e:form.add_error(None,e);status=409
        else:messages.success(request,'Назначение сохранено. Доступ к объекту и документам не изменён.');return redirect('equipment_detail',pk=pk)
    return render(request,'projects/equipment_form.html',{'form':form,'item':item,'title':'Назначить / перевести технику'},status=status)

@permission_required('equipment.manage')
def correct_start(request,pk):
    row=get_object_or_404(service.placements(request.user),pk=pk)
    form=StartForm(request.POST or None,initial={'expected_version':row.version,'start_date':row.start_date});status=200
    if request.method=='POST' and form.is_valid():
        try:service.extend_start(request.user,pk,form.cleaned_data['expected_version'],form.cleaned_data['start_date'],form.cleaned_data['reason'])
        except ValidationError as e:form.add_error(None,e);status=409
        else:
            messages.success(request,'Начало работы исправлено. Тарифы, платежи и внесённая наработка сохранены. Для добавленных дней проверьте тариф и заполните табель.')
            return redirect('equipment_detail',pk=row.equipment_id)
    return render(request,'projects/equipment_form.html',{'form':form,'item':row.equipment,'title':'Указать более раннее начало работы'},status=status)

@permission_required('equipment.manage')
def end(request,pk):
    row=get_object_or_404(service.placements(request.user),pk=pk)
    form=EndForm(request.POST or None,initial={'expected_version':row.version,'end_date':timezone.localdate()});status=200
    if request.method=='POST' and form.is_valid():
        try:service.end_assignment(request.user,pk,form.cleaned_data['expected_version'],form.cleaned_data['end_date'])
        except ValidationError as e:form.add_error(None,e);status=409
        else:return redirect('equipment_detail',pk=row.equipment_id)
    return render(request,'projects/equipment_form.html',{'form':form,'item':row.equipment,'title':'Завершить назначение'},status=status)

@permission_required('equipment.manage')
def file_edit(request,equipment_id,pk=None):
    item=get_object_or_404(service.visible_equipment(request.user),pk=equipment_id)
    row=get_object_or_404(service.equipment_files(request.user),pk=pk,equipment=item) if pk else None
    form=FileForm(request.POST or None,instance=row,user=request.user,initial={'expected_version':row.version} if row else None);status=200
    if request.method=='POST' and form.is_valid():
        try:service.save_file(request.user,equipment_id,pk,form.cleaned_data.get('expected_version'),form.cleaned_data)
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:return redirect('equipment_detail',pk=equipment_id)
    return render(request,'projects/equipment_form.html',{'form':form,'item':item,'title':'Документ техники','document_form':True},status=status)
