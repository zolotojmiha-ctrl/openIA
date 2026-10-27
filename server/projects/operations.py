"""Explicit operational actions; customer data crosses a publication boundary only."""
from datetime import timedelta
from decimal import Decimal
import io,zipfile
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse,Http404
from django.shortcuts import get_object_or_404,render,redirect
from django.urls import reverse
from django.utils import timezone
from accounts.models import User
from accounts.permissions import permission_required,permitted
from accounts.views import leader_required,audit
from documents.models import Person,PersonAssignment,DocumentVersion
from documents.access import visible_people,visible_documents
from documents.storage import verified_file
from .models import (Equipment,EquipmentCustody,Brigade,Admission,AdmissionRevision,AdmissionFile,
 CustomerAccess,CustomerPublication,MaterialRequest,Contract,ProjectObject)
from .access import visible_objects,visible_contracts
from .crews import visible_crews
from .equipment import visible_equipment
DATE=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'})

def page(request,form,title,back,**extra):
    return render(request,'projects/operation_form.html',dict(form=form,title=title,back=back,**extra))

def scope(user,contract_id,object_id):
    c=get_object_or_404(visible_contracts(user).exclude(status='archived'),pk=contract_id)
    o=get_object_or_404(visible_objects(user).filter(contracts=c).exclude(status='archived'),pk=object_id)
    return c,o

def lock_actor(user,permission=None):
    actor=User.objects.select_for_update().get(pk=user.pk)
    if not actor.is_active or actor.session_version!=user.session_version:raise PermissionDenied
    if permission and not permitted(actor,permission):raise PermissionDenied
    if permission is None and not actor.is_leader:raise PermissionDenied
    return actor

def check_version(row,value):
    if row.version!=value:raise ValidationError('Запись уже изменена. Обновите страницу и повторите.')

def check_dates(start,end):
    if end and end<start:raise ValidationError('Окончание раньше начала.')

class CustodyForm(forms.ModelForm):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    class Meta:
        model=EquipmentCustody;fields=['brigade','driver','start_date','end_date'];widgets={'start_date':DATE,'end_date':DATE}
    def __init__(self,*a,user,**kw):
        super().__init__(*a,**kw)
        self.fields['brigade'].queryset=visible_crews(user).filter(archived=False)
        self.fields['driver'].queryset=visible_people(user).filter(archived=False)
    def clean(self):
        d=super().clean()
        if not d.get('brigade') and not d.get('driver'):raise ValidationError('Выберите бригаду, водителя или обоих.')
        if d.get('start_date'):check_dates(d['start_date'],d.get('end_date'))
        return d

@permission_required('equipment.manage')
def custody(request,pk):
    item=get_object_or_404(visible_equipment(request.user),pk=pk)
    form=CustodyForm(request.POST or None,user=request.user,initial={'start_date':timezone.localdate(),'expected_version':item.version})
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                lock_actor(request.user,'equipment.manage')
                item=Equipment.objects.select_for_update().get(pk=pk);check_version(item,form.cleaned_data['expected_version'])
                if item.status=='archived':raise ValidationError('Техника в архиве.')
                d=form.cleaned_data;over=item.custodies.filter(Q(end_date__isnull=True)|Q(end_date__gte=d['start_date']))
                if d.get('end_date'):over=over.filter(start_date__lte=d['end_date'])
                clash=Q(pk__in=[])
                if d.get('brigade'):clash|=Q(brigade__isnull=False)
                if d.get('driver'):clash|=Q(driver__isnull=False)
                if over.filter(clash).exists():raise ValidationError('В этот период уже есть закрепление бригады или водителя. Сначала завершите прежний период.')
                row=form.save(commit=False);row.equipment=item;row.created_by=request.user;row.save()
                item.version+=1;item.save(update_fields=['version'])
                audit(request.user,'equipment_assigned',kind='custody',equipment=str(pk),record=str(row.pk))
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('equipment_detail',pk=pk)
    return page(request,form,'Закрепить технику за бригадой / водителем',reverse('equipment_detail',args=[pk]),intro='Объект и контракт не требуются. Это закрепление; направление на место работ оформляется отдельно.')

class CloseForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    end_date=forms.DateField(label='Последний день',widget=DATE)

@permission_required('equipment.manage')
def custody_end(request,pk):
    row=get_object_or_404(EquipmentCustody.objects.filter(equipment__in=visible_equipment(request.user)),pk=pk)
    form=CloseForm(request.POST or None,initial={'expected_version':row.version,'end_date':timezone.localdate()})
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                lock_actor(request.user,'equipment.manage')
                Equipment.objects.select_for_update().get(pk=row.equipment_id)
                row=EquipmentCustody.objects.select_for_update().get(pk=pk);check_version(row,form.cleaned_data['expected_version'])
                end=form.cleaned_data['end_date'];check_dates(row.start_date,end)
                if row.end_date and end>row.end_date:raise ValidationError('Нельзя продлить завершённое назначение.')
                row.end_date=end;row.version+=1;row.save()
                audit(request.user,'equipment_assigned',kind='custody_end',record=str(pk),end=str(end))
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('equipment_detail',pk=row.equipment_id)
    return page(request,form,'Завершить закрепление',reverse('equipment_detail',args=[row.equipment_id]))

class AreaPersonForm(forms.Form):
    person=forms.ModelChoiceField(label='Работник из персонала',queryset=Person.objects.none())
    start_date=forms.DateField(label='Начало работы',widget=DATE)
    end_date=forms.DateField(label='Окончание',widget=DATE,required=False)
    def __init__(self,*a,user,**kw):
        super().__init__(*a,**kw);self.fields['person'].queryset=visible_people(user).filter(archived=False)
    def clean(self):
        d=super().clean()
        if d.get('start_date'):check_dates(d['start_date'],d.get('end_date'))
        return d

@permission_required('personnel.manage')
def area_person(request,contract_id,object_id):
    c,o=scope(request.user,contract_id,object_id)
    form=AreaPersonForm(request.POST or None,user=request.user,initial={'start_date':timezone.localdate()})
    back=reverse('object_detail',args=[o.pk])+'?contract='+str(c.pk)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            lock_actor(request.user,'personnel.manage')
            locked_contract=Contract.objects.select_for_update().get(pk=c.pk)
            if locked_contract.status=='archived' or not locked_contract.objects_linked.filter(pk=o.pk).exists():raise PermissionDenied
            person=Person.objects.select_for_update().get(pk=form.cleaned_data['person'].pk)
            if person.archived:raise PermissionDenied
            d=form.cleaned_data
            overlap=PersonAssignment.objects.filter(person=person,project_object=o).filter(Q(end_date__isnull=True)|Q(end_date__gte=d['start_date']))
            if d['end_date']:overlap=overlap.filter(Q(start_date__isnull=True)|Q(start_date__lte=d['end_date']))
            if overlap.exists():form.add_error(None,'Работник уже назначен на этот участок в выбранный период, отдельно или в составе бригады.')
            else:
                row=PersonAssignment.objects.create(person=person,project_object=o,start_date=d['start_date'],end_date=d['end_date'],created_by=request.user)
                audit(request.user,'person_assigned',entity_id=str(person.pk),assignment=row.pk,contract=str(c.pk));return redirect(back)
    return page(request,form,'Назначить сотрудника · '+o.name,back,intro='Учётная запись не нужна. Назначение не выдаёт права входа в ЛК и доступа к документам.')

def packets(user):
    qs=Admission.objects.select_related('brigade','contract','project_object')
    if permitted(user,'personnel.manage'):return qs.filter(contract__in=visible_contracts(user),project_object__in=visible_objects(user))
    today=timezone.localdate()
    return qs.filter(Q(brigade__foreman__user=user)|Q(brigade__assignments__master=user,brigade__assignments__start_date__lte=today)&(Q(brigade__assignments__end_date__isnull=True)|Q(brigade__assignments__end_date__gte=today))).distinct()

CATEGORIES=[('company','СРО / документы организации'),('orders','Приказы об ответственных'),('ppr','ППР на работы'),('letters','Письма о допуске и приложения'),('safety','Охрана труда'),('profession','Удостоверения по профессиям'),('electrical','Электробезопасность'),('consent','Согласия / прочие документы')]
class AdmissionForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput,initial=0)
    contract=forms.ModelChoiceField(label='Контракт',queryset=Contract.objects.none())
    project_object=forms.ModelChoiceField(label='Участок работ',queryset=ProjectObject.objects.none())
    title=forms.CharField(label='Комплект / виды работ',max_length=250)
    people=forms.ModelMultipleChoiceField(label='Персонал в этом комплекте',queryset=Person.objects.none(),widget=forms.CheckboxSelectMultiple)
    equipment=forms.ModelMultipleChoiceField(label='Техника в этом комплекте',queryset=Equipment.objects.none(),required=False,widget=forms.CheckboxSelectMultiple)
    status=forms.ChoiceField(label='Состояние комплекта',choices=AdmissionRevision._meta.get_field('status').choices)
    start_date=forms.DateField(label='Начало допуска',widget=DATE)
    end_date=forms.DateField(label='Окончание допуска',widget=DATE,required=False)
    instruction_date=forms.DateField(label='Дата инструктажа',widget=DATE,required=False)
    instruction_place=forms.CharField(label='Место инструктажа / НПС',max_length=200,required=False)
    note=forms.CharField(label='Основание согласования / примечание',max_length=3000,required=False,widget=forms.Textarea(attrs={'rows':3}))
    def __init__(self,*a,user,**kw):
        super().__init__(*a,**kw)
        self.fields['contract'].queryset=visible_contracts(user).exclude(status='archived')
        self.fields['project_object'].queryset=visible_objects(user).exclude(status='archived')
        self.fields['people'].queryset=visible_people(user).filter(archived=False)
        self.fields['equipment'].queryset=visible_equipment(user).exclude(status='archived')
        docs=visible_documents(user).filter(archived=False,current_version__isnull=False).order_by('title')
        for key,label in CATEGORIES:self.fields['files_'+key]=forms.ModelMultipleChoiceField(label=label,queryset=docs,required=False,widget=forms.CheckboxSelectMultiple)
    def clean(self):
        d=super().clean()
        if d.get('contract') and d.get('project_object') and not d['contract'].objects_linked.filter(pk=d['project_object'].pk).exists():self.add_error('project_object','Участок не относится к выбранному контракту.')
        if d.get('start_date'):check_dates(d['start_date'],d.get('end_date'))
        if d.get('status') in ['approved','instructed'] and not d.get('note'):self.add_error('note','Укажите основание: согласованное письмо, ответ по почте или другое подтверждение.')
        selected={doc.pk:doc for key,label in CATEGORIES for doc in d.get('files_'+key,[])}
        if len(selected)>100 or sum(doc.current_version.size for doc in selected.values())>200*1024*1024:raise ValidationError('Комплект — не более 100 файлов и 200 МБ.')
        if d.get('status')=='instructed' and not d.get('instruction_date'):self.add_error('instruction_date','Укажите дату пройденного инструктажа.')
        return d

@login_required
def admission_list(request,brigade_id):
    crew=get_object_or_404(Brigade,pk=brigade_id)
    qs=packets(request.user).filter(brigade=crew)
    if not permitted(request.user,'personnel.manage') and crew.foreman.user_id!=request.user.pk and not qs.exists():raise PermissionDenied
    return render(request,'projects/admissions.html',{'crew':crew,'packets':qs,'can_edit':permitted(request.user,'personnel.manage')})

@permission_required('personnel.manage')
def admission_edit(request,brigade_id,pk=None):
    crew=get_object_or_404(visible_crews(request.user).filter(archived=False),pk=brigade_id)
    packet=get_object_or_404(packets(request.user),pk=pk,brigade=crew) if pk else None
    initial={'expected_version':packet.version if packet else 0,'start_date':timezone.localdate()}
    if packet:
        r=packet.revisions.first();initial.update(contract=packet.contract_id,project_object=packet.project_object_id,title=packet.title,people=[p['id'] for p in r.snapshot['people']],equipment=[p['id'] for p in r.snapshot['equipment']])
        for k in ['status','start_date','end_date','instruction_date','instruction_place','note']:initial[k]=getattr(r,k)
        for key,label in CATEGORIES:initial['files_'+key]=[f.version.document_id for f in r.files.select_related('version').filter(category=key)]
    else:
        initial['people']=list(crew.memberships.filter(start_date__lte=timezone.localdate()).filter(Q(end_date__isnull=True)|Q(end_date__gte=timezone.localdate())).values_list('person_id',flat=True))
        if crew.foreman_id not in initial['people']:initial['people'].append(crew.foreman_id)
    form=AdmissionForm(request.POST or None,user=request.user,initial=initial)
    if packet:
        form.fields['contract'].disabled=True;form.fields['project_object'].disabled=True
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                lock_actor(request.user,'personnel.manage')
                c=Contract.objects.select_for_update().get(pk=form.cleaned_data['contract'].pk)
                if not c.objects_linked.filter(pk=form.cleaned_data['project_object'].pk).exists():raise ValidationError('Участок больше не связан с контрактом.')
                if c.status=='archived':raise ValidationError('Контракт в архиве.')
                row=Admission.objects.select_for_update().get(pk=packet.pk) if packet else Admission(brigade=crew,contract=c,project_object=form.cleaned_data['project_object'],created_by=request.user)
                check_version(row,form.cleaned_data['expected_version']);row.title=form.cleaned_data['title'];row.version+=1;row.save()
                snapshot={'people':[{'id':str(p.pk),'name':p.full_name,'position':p.position} for p in form.cleaned_data['people']], 'equipment':[{'id':str(e.pk),'name':e.name,'registration':e.registration_number} for e in form.cleaned_data['equipment']]}
                revision=AdmissionRevision.objects.create(admission=row,number=row.version,snapshot=snapshot,created_by=request.user,**{k:form.cleaned_data[k] for k in ['status','start_date','end_date','instruction_date','instruction_place','note']})
                used=set()
                for key,label in CATEGORIES:
                    for doc in form.cleaned_data['files_'+key]:
                        doc=visible_documents(request.user).select_for_update(of=('self',)).get(pk=doc.pk)
                        if doc.current_version_id not in used:
                            AdmissionFile.objects.create(revision=revision,version=doc.current_version,title=doc.title,category=key);used.add(doc.current_version_id)
                audit(request.user,'admission_updated',kind='admission_revision',packet=str(row.pk),revision=row.version)
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('admission_detail',pk=row.pk)
    return page(request,form,'Комплект допуска · '+crew.name,reverse('admissions',args=[crew.pk]),intro='Каждое сохранение создаёт новую редакцию. Выберите конкретные письма и ППР, а также документы каждого работника. Предыдущие письма могут дополнять друг друга. Наличие файла само по себе не подтверждает согласование или инструктаж.')

@login_required
def admission_detail(request,pk):
    packet=get_object_or_404(packets(request.user),pk=pk)
    revisions=packet.revisions.prefetch_related('files__version')
    selected=get_object_or_404(revisions,pk=request.GET['revision']) if request.GET.get('revision') else revisions.first()
    files=list(selected.files.select_related('version__document')) if selected else []
    categories={f.category for f in files}
    return render(request,'projects/admission.html',{'packet':packet,'revision':selected,'revisions':revisions,'files':files,'missing':[label for key,label in CATEGORIES if key not in categories],'can_edit':permitted(request.user,'personnel.manage')})

def safe_name(value):
    import re
    return re.sub(r'[^\w .()—-]','_',value)[:160] or 'document'

@login_required
def _admission_download(request,pk,revision_id,file_id=None):
    packet=get_object_or_404(packets(request.user),pk=pk)
    revision=get_object_or_404(packet.revisions,pk=revision_id)
    files=revision.files.select_related('version__document')
    if file_id:
        f=get_object_or_404(files,pk=file_id)
        return FileResponse(verified_file(f.version),as_attachment=True,filename=f.version.original_name,content_type=f.version.content_type)
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w',zipfile.ZIP_DEFLATED) as z:
        for i,f in enumerate(files,1):
            with verified_file(f.version) as stream:z.writestr(f'{i:03d} '+safe_name(f.title)+'.'+safe_name(f.version.original_name.rsplit('.',1)[-1])[:10],stream.read())
        import json
        z.writestr('Состав комплекта.json',json.dumps({'title':packet.title,'revision':revision.number,'people':revision.snapshot['people'],'equipment':revision.snapshot['equipment']},ensure_ascii=False,indent=2))
    data.seek(0);audit(request.user,'document_exported',kind='admission',packet=str(pk),revision=revision.number)
    return FileResponse(data,as_attachment=True,filename='Комплект допуска.zip')

class CustomerGrantForm(forms.Form):
    user=forms.ModelChoiceField(label='Представитель заказчика',queryset=User.objects.none())
    contract=forms.ModelChoiceField(label='Контракт',queryset=Contract.objects.none())
    objects=forms.ModelMultipleChoiceField(label='Разрешённые участки',queryset=ProjectObject.objects.none(),widget=forms.CheckboxSelectMultiple)
    def __init__(self,*a,user,**kw):
        super().__init__(*a,**kw);self.fields['user'].queryset=User.objects.filter(role='customer',is_active=True,leader_slot__isnull=True)
        self.fields['contract'].queryset=visible_contracts(user);self.fields['objects'].queryset=visible_objects(user)
    def clean(self):
        d=super().clean()
        if d.get('contract') and d.get('objects') and d['objects'].exclude(contracts=d['contract']).exists():self.add_error('objects','Все участки должны принадлежать выбранному контракту.')
        return d

@leader_required
def customer_access(request):
    form=CustomerGrantForm(request.POST or None,user=request.user)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            lock_actor(request.user)
            user=User.objects.select_for_update().get(pk=form.cleaned_data['user'].pk)
            if user.role!='customer' or not user.is_active:raise PermissionDenied
            c=form.cleaned_data['contract'];CustomerAccess.objects.filter(user=user,contract=c).delete()
            for obj in form.cleaned_data['objects']:CustomerAccess.objects.create(user=user,contract=c,project_object=obj,created_by=request.user)
            audit(request.user,'contract_access_changed',user,kind='customer',contract=str(c.pk))
        messages.success(request,'Доступ заказчика сохранён. Для этого контракта список участков заменён.');return redirect('customer_access')
    return page(request,form,'Доступ заказчика',reverse('users'),intro='Сначала создайте пользователя с ролью «Заказчик». Его кабинет показывает только опубликованные данные выбранных участков. Повторное сохранение заменяет список участков этого контракта.',grants=CustomerAccess.objects.select_related('user','contract','project_object'))

@leader_required
def customer_revoke(request,pk):
    row=get_object_or_404(CustomerAccess,pk=pk)
    if request.method=='POST':
        audit(request.user,'contract_access_changed',row.user,kind='customer_revoke',contract=str(row.contract_id));row.delete();return redirect('customer_access')
    return page(request,forms.Form(),'Отозвать доступ заказчика к участку',reverse('customer_access'))

class MaterialForm(forms.ModelForm):
    expected_version=forms.IntegerField(widget=forms.HiddenInput,initial=0)
    class Meta:
        model=MaterialRequest;fields=['name','quantity','unit','kilometre','needed_on','note','status'];widgets={'needed_on':DATE}
    def clean_quantity(self):
        n=self.cleaned_data['quantity']
        if n<=0:raise ValidationError('Количество должно быть больше нуля.')
        return n

@permission_required('repairs.prepare')
def material_edit(request,contract_id,object_id,pk=None):
    c,o=scope(request.user,contract_id,object_id)
    item=get_object_or_404(MaterialRequest,contract=c,project_object=o,pk=pk) if pk else None
    form=MaterialForm(request.POST or None,instance=item,initial={'expected_version':item.version if item else 0})
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                lock_actor(request.user,'repairs.prepare')
                c=Contract.objects.select_for_update().get(pk=c.pk)
                if c.status=='archived':raise ValidationError('Контракт в архиве.')
                if item:
                    locked=MaterialRequest.objects.select_for_update().get(pk=pk);check_version(locked,form.cleaned_data['expected_version'])
                row=form.save(commit=False);row.contract=c;row.project_object=o;row.created_by=item.created_by if item else request.user;row.version=(item.version+1) if item else 1;row.save()
                audit(request.user,'repair_prepared',kind='material_request',record=str(row.pk))
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('customer_publish',contract_id=c.pk,object_id=o.pk) if request.user.is_leader else redirect('work_place',pk=c.pk,object_id=o.pk)
    return page(request,form,'Потребность в материалах',reverse('work_place',args=[c.pk,o.pk]),intro='Черновик потребности. Заказчик увидит его только после публикации руководителем.')

class PublishForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    confirm=forms.BooleanField(label='Проверено: опубликовать ход работ и потребность заказчику')
    photos=forms.ModelMultipleChoiceField(label='Фотографии для заказчика',queryset=None,required=False,widget=forms.CheckboxSelectMultiple)
    def __init__(self,*a,contract,area,**kw):
        from repairs.models import Attachment
        super().__init__(*a,**kw)
        self.fields['photos'].queryset=Attachment.objects.filter(report__job__contract=contract,report__job__section__project_object=area,report__job__state='confirmed',content_type__in=['image/jpeg','image/png'])
        self.fields['photos'].label_from_instance=lambda p:p.name

@leader_required
def customer_publish(request,contract_id,object_id):
    from repairs.models import Job
    c,o=scope(request.user,contract_id,object_id)
    form=PublishForm(request.POST or None,contract=c,area=o,initial={'expected_version':c.version})
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                lock_actor(request.user)
                c=Contract.objects.select_for_update().get(pk=c.pk);check_version(c,form.cleaned_data['expected_version'])
                if c.status=='archived':raise ValidationError('Контракт в архиве.')
                jobs=list(Job.objects.filter(contract=c,section__project_object=o).select_for_update().select_related('section').prefetch_related('defects').order_by('section__pipeline','pk'))
                rows=[]
                for job in jobs:
                    for d in job.active_defects:
                        rows.append({'pipeline':job.section.pipeline,'km':d.km,'number':d.number,'state':job.get_state_display() if job.state in ['planned','progress','partial','confirmed'] else 'На внутренней проверке','method':job.get_method_display() if job.state=='confirmed' else '', 'accepted':bool(job.accepted_date) if job.state=='confirmed' else False})
                needs=[{'name':m.name,'quantity':str(m.quantity),'unit':m.unit,'km':m.kilometre,'date':str(m.needed_on or ''),'note':m.note,'status':m.get_status_display()} for m in MaterialRequest.objects.filter(contract=c,project_object=o)]
                photos=[str(p.pk) for p in form.cleaned_data['photos'] if p.report.job.state=='confirmed']
                CustomerPublication.objects.create(contract=c,project_object=o,snapshot={'contract':c.number,'area':o.name,'rows':rows,'materials':needs,'photos':photos},created_by=request.user)
                c.version+=1;c.save(update_fields=['version','updated_at'])
                audit(request.user,'customer_published',kind='customer_publication',contract=str(c.pk),area=str(o.pk))
        except ValidationError as e:form.add_error(None,e)
        else:messages.success(request,'Опубликовано в кабинете заказчика.');return redirect('customer_publish',contract_id=c.pk,object_id=o.pk)
    return page(request,form,'Публикация для заказчика · '+o.name,reverse('work_place',args=[c.pk,o.pk]),intro='Публикуются только перечисленные поля: нефтепровод, км, номер дефекта, состояние работ, проверенный способ устранения, материалы и выбранные фото. Внутренние примечания и персональные документы не публикуются.',needs=MaterialRequest.objects.filter(contract=c,project_object=o),contract_id=c.pk,object_id=o.pk)

@login_required
def customer_portal(request):
    if request.user.role!='customer' or request.user.is_leader:raise PermissionDenied
    rows=[]
    for grant in CustomerAccess.objects.filter(user=request.user).select_related('contract','project_object').order_by('contract__number','project_object__name'):
        publication=CustomerPublication.objects.filter(contract=grant.contract,project_object=grant.project_object).first()
        rows.append({'grant':grant,'publication':publication})
    return render(request,'projects/customer_portal.html',{'rows':rows})

@login_required
def customer_photo(request,publication_id,pk):
    from repairs.models import Attachment
    publication=get_object_or_404(CustomerPublication,pk=publication_id)
    if request.user.role!='customer' or not CustomerAccess.objects.filter(user=request.user,contract=publication.contract,project_object=publication.project_object).exists():raise Http404
    if str(pk) not in publication.snapshot.get('photos',[]):raise Http404
    # Old publications are not an alternative route around corrections/revocation.
    if CustomerPublication.objects.filter(contract=publication.contract,project_object=publication.project_object).first().pk!=publication.pk:raise Http404
    photo=get_object_or_404(Attachment,pk=pk,report__job__contract=publication.contract,report__job__section__project_object=publication.project_object,report__job__state='confirmed')
    response=FileResponse(verified_file(photo),content_type=photo.content_type)
    response['Content-Disposition']='inline';return response

@login_required
def my_admissions(request):
    return render(request,'projects/my_admissions.html',{'packets':packets(request.user)})

@leader_required
def withdraw_publication(request,contract_id,object_id):
    c,o=scope(request.user,contract_id,object_id)
    if request.method=='POST':
        with transaction.atomic():
            lock_actor(request.user)
            c=Contract.objects.select_for_update().get(pk=c.pk)
            if c.status=='archived':raise PermissionDenied
            CustomerPublication.objects.create(contract=c,project_object=o,snapshot={'withdrawn':True,'contract':c.number,'area':o.name,'rows':[],'materials':[],'photos':[]},created_by=request.user)
            c.version+=1;c.save(update_fields=['version','updated_at'])
            audit(request.user,'customer_published',kind='withdrawn',contract=str(c.pk),area=str(o.pk))
        messages.success(request,'Публикация снята. История сохранена.')
    return redirect('customer_publish',contract_id=c.pk,object_id=o.pk)

@login_required
def admission_download(request,pk,revision_id,file_id=None):
    try:return _admission_download(request,pk,revision_id,file_id)
    except OSError:
        from django.http import HttpResponse
        audit(request.user,'document_integrity_error',kind='admission',packet=str(pk))
        return HttpResponse('Не удалось проверить файл комплекта. Обратитесь к руководителю.',status=503)
