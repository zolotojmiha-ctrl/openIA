from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.contrib import messages
from django.shortcuts import render,redirect,get_object_or_404
from django.utils import timezone
from accounts.permissions import permission_required
from accounts.views import audit
from documents.access import visible_people
from documents.models import Person
from .models import Brigade,BrigadeMember,BrigadeAssignment,Contract,ProjectObject
from .access import visible_contracts,visible_objects,contract_card
from .crews import visible_crews,visible_assignments,create_crew,add_member,close_member,assign_crew_place,close_assignment,assign_jobs_to_crew
from repairs.access import prepare_required,visible_jobs

DATE=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'})
class CrewForm(forms.Form):
    name=forms.CharField(label='Название бригады',max_length=160)
    foreman=forms.ModelChoiceField(label='Мастер',queryset=Person.objects.none())
    members=forms.ModelMultipleChoiceField(label='Сотрудники бригады',queryset=Person.objects.none(),required=False,widget=forms.CheckboxSelectMultiple)
    start=forms.DateField(label='Начало участия',widget=DATE)
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs);people=visible_people(user).filter(archived=False).order_by('full_name');self.fields['foreman'].queryset=people;self.fields['members'].queryset=people;self.initial['start']=timezone.localdate()

class MemberForm(forms.Form):
    version=forms.IntegerField(widget=forms.HiddenInput)
    person=forms.ModelChoiceField(label='Сотрудник',queryset=Person.objects.none())
    start=forms.DateField(label='Начало участия',widget=DATE)
    end=forms.DateField(label='Окончание участия',widget=DATE,required=False)
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs);self.fields['person'].queryset=visible_people(user).filter(archived=False).order_by('full_name')

@permission_required('personnel.manage')
def crews(request):
    return render(request,'projects/crews.html',{'crews':visible_crews(request.user).order_by('name')})

@permission_required('personnel.manage')
def crew_new(request):
    form=CrewForm(request.POST or None,user=request.user)
    if request.method=='POST' and form.is_valid():
        try:crew=create_crew(request.user,form.cleaned_data['name'],form.cleaned_data['foreman'],list(form.cleaned_data['members']),form.cleaned_data['start'])
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('brigade_detail',pk=crew.pk)
    return render(request,'projects/crew_form.html',{'form':form,'title':'Создать бригаду'})

@permission_required('personnel.manage')
def crew_detail(request,pk):
    crew=get_object_or_404(visible_crews(request.user),pk=pk)
    form=MemberForm(request.POST or None,user=request.user,initial={'version':crew.version,'start':timezone.localdate()})
    if request.method=='POST' and form.is_valid():
        try:add_member(request.user,pk,form.cleaned_data['version'],form.cleaned_data['person'],form.cleaned_data['start'],form.cleaned_data['end'])
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('brigade_detail',pk=pk)
    return render(request,'projects/crew_detail.html',{'crew':crew,'members':crew.memberships.select_related('person').order_by('-start_date','pk'),'assignments':visible_assignments(request.user).filter(brigade=crew),'form':form})

class EndForm(forms.Form):
    version=forms.IntegerField(widget=forms.HiddenInput)
    end=forms.DateField(label='Дата окончания',widget=DATE)

@permission_required('personnel.manage')
def end_member(request,pk):
    member=get_object_or_404(BrigadeMember.objects.filter(brigade__in=visible_crews(request.user)),pk=pk)
    form=EndForm(request.POST or None,initial={'version':member.version,'end':timezone.localdate()})
    if request.method=='POST' and form.is_valid():
        try:close_member(request.user,pk,form.cleaned_data['version'],form.cleaned_data['end'])
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('brigade_detail',pk=member.brigade_id)
    return render(request,'projects/crew_form.html',{'form':form,'title':'Завершить участие · '+member.person.full_name})

class PlaceAssignmentForm(forms.Form):
    brigade=forms.ModelChoiceField(label='Бригада',queryset=Brigade.objects.none())
    project_object=forms.ModelChoiceField(label='Место выполнения',queryset=ProjectObject.objects.none())
    start=forms.DateField(label='Начало работы',widget=DATE)
    end=forms.DateField(label='Окончание работы',widget=DATE,required=False)
    def __init__(self,*args,user,contract,**kwargs):
        super().__init__(*args,**kwargs);self.fields['brigade'].queryset=visible_crews(user).filter(archived=False);self.fields['project_object'].queryset=visible_objects(user).filter(contracts=contract);self.initial['start']=timezone.localdate()

@permission_required('personnel.manage')
def contract_crews(request,pk):
    contract=get_object_or_404(visible_contracts(request.user),pk=pk)
    form=PlaceAssignmentForm(request.POST or None,user=request.user,contract=contract)
    versions={str(c.pk):c.version for c in form.fields['brigade'].queryset}
    snapshot=signing.dumps(versions,salt='crew-place-versions')
    if request.method=='POST' and form.is_valid():
        try:
            shown=signing.loads(request.POST.get('snapshot',''),salt='crew-place-versions',max_age=3600)
            crew=form.cleaned_data['brigade'];version=shown.get(str(crew.pk))
            if version is None:raise ValidationError('Бригада отсутствовала в выборе. Обновите страницу.')
            assign_crew_place(request.user,crew.pk,version,contract.pk,form.cleaned_data['project_object'].pk,form.cleaned_data['start'],form.cleaned_data['end'])
        except (signing.BadSignature,ValidationError) as e:form.add_error(None,e if isinstance(e,ValidationError) else 'Выбор устарел. Обновите страницу.')
        else:messages.success(request,'Бригада назначена. Состав и даты сохранены для каждого сотрудника.');return redirect('contract_crews',pk=pk)
    return render(request,'projects/contract_crews.html',{'card':contract_card(contract,request.user),'assignments':visible_assignments(request.user).filter(contract=contract).order_by('-start_date'),'form':form,'snapshot':snapshot})

@permission_required('personnel.manage')
def end_assignment(request,pk):
    item=get_object_or_404(visible_assignments(request.user),pk=pk)
    form=EndForm(request.POST or None,initial={'version':item.version,'end':timezone.localdate()})
    if request.method=='POST' and form.is_valid():
        try:close_assignment(request.user,pk,form.cleaned_data['version'],form.cleaned_data['end'])
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('contract_crews',pk=item.contract_id)
    return render(request,'projects/crew_form.html',{'form':form,'title':'Завершить назначение · '+item.brigade.name})

class AssignJobsForm(forms.Form):
    snapshot=forms.CharField(widget=forms.HiddenInput)
    jobs=forms.ModelMultipleChoiceField(label='Работы',queryset=None,widget=forms.CheckboxSelectMultiple)
    def __init__(self,*args,user,assignment,**kwargs):
        super().__init__(*args,**kwargs)
        qs=visible_jobs(user).filter(contract=assignment.contract,section__project_object=assignment.project_object).exclude(state='confirmed').order_by('pk')
        self.fields['jobs'].queryset=qs;self.fields['jobs'].label_from_instance=lambda j:', '.join(str(d) for d in j.active_defects)
        self.initial['snapshot']=signing.dumps({str(j.pk):j.version for j in qs},salt='crew-jobs')

@prepare_required
def crew_jobs(request,pk):
    assignment=get_object_or_404(visible_assignments(request.user),pk=pk)
    form=AssignJobsForm(request.POST or None,user=request.user,assignment=assignment)
    if request.method=='POST' and form.is_valid():
        try:
            snapshot=signing.loads(form.cleaned_data['snapshot'],salt='crew-jobs',max_age=3600);ids=[str(j.pk) for j in form.cleaned_data['jobs']]
            if any(i not in snapshot for i in ids):raise ValidationError('Выбор изменился.')
            assign_jobs_to_crew(request.user,pk,{i:snapshot[i] for i in ids})
        except (signing.BadSignature,ValidationError) as e:form.add_error(None,e if isinstance(e,ValidationError) else 'Выбор устарел.')
        else:messages.success(request,'Работы назначены бригаде.');return redirect('work_place',pk=assignment.contract_id,object_id=assignment.project_object_id)
    return render(request,'projects/crew_form.html',{'form':form,'title':'Назначить работы · '+assignment.brigade.name})

class JobCrewForm(forms.Form):
    version=forms.IntegerField(widget=forms.HiddenInput)
    assignment=forms.ModelChoiceField(label='Назначение бригады',queryset=BrigadeAssignment.objects.none())
    def __init__(self,*args,user,job,**kwargs):
        super().__init__(*args,**kwargs);self.fields['assignment'].queryset=visible_assignments(user).filter(contract=job.contract,project_object=job.section.project_object).filter(Q(end_date__isnull=True)|Q(end_date__gte=timezone.localdate())) if job.contract_id else BrigadeAssignment.objects.none()

from django.db.models import Q
@prepare_required
def job_crew(request,pk):
    job=get_object_or_404(visible_jobs(request.user),pk=pk)
    form=JobCrewForm(request.POST or None,user=request.user,job=job,initial={'version':job.version})
    if request.method=='POST' and form.is_valid():
        try:assign_jobs_to_crew(request.user,form.cleaned_data['assignment'].pk,{str(job.pk):form.cleaned_data['version']})
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('repair_detail',pk=pk)
    return render(request,'projects/crew_form.html',{'form':form,'title':'Назначить бригаду для ремонта','job':job})

class CrewEditForm(forms.Form):
    version=forms.IntegerField(widget=forms.HiddenInput)
    name=forms.CharField(label='Название бригады',max_length=160)
    foreman=forms.ModelChoiceField(label='Мастер',queryset=Person.objects.none())
    archived=forms.BooleanField(label='Бригада в архиве',required=False)
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs);self.fields['foreman'].queryset=visible_people(user).filter(archived=False)

@permission_required('personnel.manage')
def crew_edit(request,pk):
    from .crews import update_crew
    crew=get_object_or_404(visible_crews(request.user),pk=pk)
    form=CrewEditForm(request.POST or None,user=request.user,initial={'version':crew.version,'name':crew.name,'foreman':crew.foreman_id,'archived':crew.archived})
    if request.method=='POST' and form.is_valid():
        try:update_crew(request.user,pk,form.cleaned_data['version'],form.cleaned_data['name'],form.cleaned_data['foreman'],form.cleaned_data['archived'])
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('brigade_detail',pk=pk)
    return render(request,'projects/crew_form.html',{'title':'Настройки бригады','form':form})
