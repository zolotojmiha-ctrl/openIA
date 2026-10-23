import uuid
from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core import signing
from django.db.models import Q
from django.http import FileResponse,Http404
from django.shortcuts import get_object_or_404,render,redirect
from django.utils import timezone
from accounts.models import User
from accounts.permissions import permission_required,permitted
from accounts.views import leader_required
from documents.models import Document
from documents.access import visible_documents
from documents.storage import verified_file
from repairs.forms import MultipleFiles,MultipleInput
from repairs.access import visible_jobs,can_prepare
from repairs.models import Job
from .models import Subcontractor,SubcontractOrder,SubcontractRepresentative,SubcontractOrderAccess,SubcontractDocument,SubcontractAttachment,WORK_KINDS
from .access import visible_objects,visible_contracts,contract_card
from . import subcontracts as service

DATE=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'})
class CompanyForm(forms.ModelForm):
    expected_version=forms.IntegerField(widget=forms.HiddenInput,required=False)
    class Meta:
        model=Subcontractor;fields=['name','tax_id','contact','notes','archived'];widgets={'notes':forms.Textarea(attrs={'rows':3})}

class OrderForm(forms.ModelForm):
    expected_version=forms.IntegerField(widget=forms.HiddenInput,required=False)
    class Meta:
        model=SubcontractOrder;fields=['company','contract','project_object','kind','title','scope','planned_quantity','unit','responsible','archived'];widgets={'scope':forms.Textarea(attrs={'rows':3})};labels={'company':'Субподрядчик','contract':'Наш контракт','project_object':'Объект'}
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs);self.fields['company'].queryset=service.companies(user).filter(archived=False);self.fields['contract'].queryset=visible_contracts(user).exclude(status='archived');self.fields['project_object'].queryset=visible_objects(user).exclude(status='archived');self.fields['responsible'].queryset=User.objects.filter(is_active=True).exclude(role='subcontractor').order_by('full_name')
        self.fields['contract'].label_from_instance=lambda c:contract_card(c,user)['title'];self.fields['responsible'].label_from_instance=lambda u:u.full_name

class RepForm(forms.Form):
    expected_version=forms.IntegerField(required=False,widget=forms.HiddenInput)
    user=forms.ModelChoiceField(label='Учётная запись представителя',queryset=User.objects.none())
    active=forms.BooleanField(label='Действует',required=False,initial=True)
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.fields['user'].queryset=User.objects.filter(role='subcontractor',leader_slot__isnull=True).order_by('full_name');self.fields['user'].label_from_instance=lambda u:u.full_name+(' · вход закрыт' if not u.is_active else '')

class OrderAccessForm(forms.Form):
    representative=forms.ModelChoiceField(label='Представитель компании',queryset=SubcontractRepresentative.objects.none())
    active=forms.BooleanField(label='Просмотр поручения',required=False,initial=True)
    can_report=forms.BooleanField(label='Разрешить внесение результата',required=False)
    expected_version=forms.IntegerField(required=False,widget=forms.HiddenInput)
    def __init__(self,*args,order,**kwargs):
        super().__init__(*args,**kwargs);self.fields['representative'].queryset=order.company.representatives.select_related('user')

class DocumentForm(forms.ModelForm):
    expected_version=forms.IntegerField(required=False,widget=forms.HiddenInput)
    class Meta:
        model=SubcontractDocument;fields=['order','document','label','active'];labels={'order':'Поручение (пусто — документ компании)','document':'Документ'}
    def __init__(self,*args,user,company,**kwargs):
        super().__init__(*args,**kwargs);self.fields['document'].queryset=visible_documents(user);self.fields['order'].queryset=service.orders(user,True).filter(company=company)
        if not self.instance._state.adding:
            self.fields['document'].disabled=True;self.fields['order'].disabled=True

class ResultForm(forms.Form):
    token=forms.UUIDField(widget=forms.HiddenInput,initial=uuid.uuid4)
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    outcome=forms.ChoiceField(label='Результат',choices=[('complete','Выполнено'),('partial','Выполнено частично'),('problem','Не смогли выполнить / препятствие')])
    quantity=forms.DecimalField(label='Выполнено с начала работ (не за день)',max_digits=12,decimal_places=3,min_value=0,required=False)
    date=forms.DateField(label='Дата',widget=DATE,initial=timezone.localdate)
    note=forms.CharField(label='Что выполнено / причина препятствия',max_length=3000,widget=forms.Textarea(attrs={'rows':3}))
    files=MultipleFiles(label='Фото и подтверждающие файлы',required=False,widget=MultipleInput())

class DecisionForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    decision=forms.ChoiceField(label='Решение',choices=[('confirmed','Подтвердить'),('revision','Вернуть на доработку')])
    note=forms.CharField(label='Что исправить / пояснение',required=False,max_length=3000,widget=forms.Textarea(attrs={'rows':3}))

@login_required
def home(request):
    companies=service.companies(request.user).filter(archived=request.GET.get('archive')=='1').order_by('name')
    orders=service.orders(request.user).order_by('title')
    if request.GET.get('contract'):
        try:orders=orders.filter(contract_id=uuid.UUID(request.GET['contract']))
        except ValueError:orders=orders.none()
        companies=companies.filter(pk__in=orders.values('company_id'))
    if request.GET.get('review')=='1':orders=orders.filter(Q(state='reported')|Q(repair_jobs__state='reported')).distinct()
    return render(request,'projects/subcontract_home.html',{'companies':companies,'page':Paginator(orders,30).get_page(request.GET.get('page')),'archive':request.GET.get('archive')=='1'})

@login_required
def company_detail(request,pk):
    company=get_object_or_404(service.companies(request.user),pk=pk)
    orders=service.orders(request.user,True).filter(company=company).order_by('title')
    return render(request,'projects/subcontract_company.html',{'company':company,'orders':orders,'files':service.files(request.user).filter(company=company,order__isnull=True),'representatives':company.representatives.select_related('user') if permitted(request.user,'subcontract.manage') else []})

@permission_required('subcontract.manage')
def company_edit(request,pk=None):
    company=get_object_or_404(service.companies(request.user),pk=pk) if pk else None;form=CompanyForm(request.POST or None,instance=company,initial={'expected_version':company.version} if company else None);status=200
    if request.method=='POST' and form.is_valid():
        try:row=service.save_company(request.user,pk,form.cleaned_data.get('expected_version'),form.cleaned_data)
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:return redirect('subcontract_company',pk=row.pk)
    return render(request,'projects/subcontract_form.html',{'form':form,'title':'Изменить компанию / архив' if company else 'Добавить компанию-субподрядчика','company':company},status=status)

@permission_required('subcontract.manage')
def order_edit(request,pk=None):
    order=get_object_or_404(service.orders(request.user,True),pk=pk) if pk else None
    initial={'expected_version':order.version} if order else {}
    for key in ('company','contract','project_object'):
        try:initial.setdefault(key,uuid.UUID(request.GET.get(key,'')))
        except ValueError:pass
    form=OrderForm(request.POST or None,user=request.user,instance=order,initial=initial);status=200
    if request.method=='POST' and form.is_valid():
        try:row=service.save_order(request.user,pk,form.cleaned_data.get('expected_version'),form.cleaned_data)
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:return redirect('subcontract_order',pk=row.pk)
    return render(request,'projects/subcontract_form.html',{'form':form,'title':'Изменить поручение / архив' if order else 'Поручить работы субподрядчику'},status=status)

@login_required
def order_detail(request,pk):
    order=get_object_or_404(service.orders(request.user,True),pk=pk)
    repair_jobs=visible_jobs(request.user).filter(subcontract_order=order)
    return render(request,'projects/subcontract_order.html',{'order':order,'card':contract_card(order.contract,request.user),'jobs':repair_jobs,'can_report_order':service.can_report(request.user,order),'can_link_repairs':order.contract.status!='archived' and permitted(request.user,'subcontract.manage') and can_prepare(request.user),'files':service.files(request.user).filter(company=order.company).filter(Q(order=order)|Q(order__isnull=True)),'latest_result':order.results.select_related('decision').first(),'results':order.results.select_related('author','decision__actor').prefetch_related('attachments'),'access_rules':order.access_rules.select_related('representative__user') if permitted(request.user,'subcontract.manage') else []})

@permission_required('subcontract.manage')
def representative(request,company_id,pk=None):
    company=get_object_or_404(service.companies(request.user),pk=company_id);row=get_object_or_404(company.representatives,pk=pk) if pk else None
    form=RepForm(request.POST or None,initial={'expected_version':row.version,'user':row.user_id,'active':row.active} if row else None);status=200
    if row:
        form.fields['user'].queryset=User.objects.filter(Q(role='subcontractor')|Q(pk=row.user_id));form.fields['user'].disabled=True
    if request.method=='POST' and form.is_valid():
        c=form.cleaned_data
        try:service.save_representative(request.user,company_id,pk,c['expected_version'],c['user'],c['active'])
        except ValidationError as e:form.add_error(None,'; '.join(e.messages));status=409
        else:return redirect('subcontract_company',pk=company_id)
    return render(request,'projects/subcontract_form.html',{'form':form,'title':'Представитель компании','company':company,'representative_form':True},status=status)

@permission_required('subcontract.manage')
def order_access(request,pk):
    order=get_object_or_404(service.orders(request.user,True),pk=pk)
    initial={}
    try:
        rep=order.company.representatives.get(pk=int(request.GET.get('representative','')))
        initial['representative']=rep;existing=order.access_rules.filter(representative=rep).first()
        if existing:initial.update(expected_version=existing.version,active=existing.active,can_report=existing.can_report)
    except (ValueError,SubcontractRepresentative.DoesNotExist):pass
    form=OrderAccessForm(request.POST or None,order=order,initial=initial);status=200
    if request.method=='POST' and form.is_valid():
        c=form.cleaned_data
        try:service.grant_order(request.user,pk,c['representative'].pk,c['active'],c['can_report'],c['expected_version'])
        except ValidationError as e:form.add_error(None,e);status=409
        else:return redirect('subcontract_order',pk=pk)
    return render(request,'projects/subcontract_form.html',{'form':form,'title':'Доступ к поручению','order':order,'access_form':True},status=status)

@permission_required('subcontract.manage')
def document_link(request,company_id,pk=None):
    company=get_object_or_404(service.companies(request.user),pk=company_id);row=get_object_or_404(service.files(request.user),pk=pk,company=company) if pk else None
    form=DocumentForm(request.POST or None,user=request.user,company=company,instance=row,initial={'expected_version':row.version} if row else None);status=200
    if request.method=='POST' and form.is_valid():
        try:service.save_document(request.user,company_id,pk,form.cleaned_data.get('expected_version'),form.cleaned_data)
        except ValidationError as e:form.add_error(None,e);status=409
        else:return redirect('subcontract_company',pk=company_id)
    return render(request,'projects/subcontract_form.html',{'form':form,'company':company,'title':'Связать документ','document_form':True},status=status)

class RepairLinksForm(forms.Form):
    jobs=forms.ModelMultipleChoiceField(label='Выберите ремонты',queryset=Job.objects.none(),widget=forms.CheckboxSelectMultiple)
    snapshot=forms.CharField(widget=forms.HiddenInput)
    def __init__(self,*args,user,order,**kwargs):
        super().__init__(*args,**kwargs);self.fields['jobs'].queryset=visible_jobs(user).filter(contract=order.contract,section__project_object=order.project_object).exclude(state='confirmed').filter(Q(subcontract_order__isnull=True)|Q(subcontract_order=order))
        self.fields['jobs'].label_from_instance=lambda j:'; '.join('км '+d.km+' № '+d.number for d in j.active_defects)

@permission_required('subcontract.manage')
def repair_links(request,pk):
    if not can_prepare(request.user):raise Http404
    order=get_object_or_404(service.orders(request.user),pk=pk);form=RepairLinksForm(request.POST or None,user=request.user,order=order);status=200
    if request.method=='POST' and form.is_valid():
        try:
            payload=signing.loads(form.cleaned_data['snapshot'],salt='subcontract-repairs',max_age=3600)
            if payload['actor']!=str(request.user.pk) or payload['order']!=str(order.pk):raise ValidationError('Выбор изменён.')
            versions={str(j.pk):payload['versions'][str(j.pk)] for j in form.cleaned_data['jobs']};service.link_repairs(request.user,pk,versions)
        except (ValidationError,signing.BadSignature,KeyError) as e:form.add_error(None,e if isinstance(e,ValidationError) else 'Выбор устарел.');status=409
        else:return redirect('subcontract_order',pk=pk)
    form.initial['snapshot']=signing.dumps({'actor':str(request.user.pk),'order':str(order.pk),'versions':{str(j.pk):j.version for j in form.fields['jobs'].queryset}},salt='subcontract-repairs',compress=True)
    return render(request,'projects/subcontract_form.html',{'form':form,'order':order,'title':'Передать дефекты субподрядчику'},status=status)

@login_required
def result(request,pk,accept=False):
    order=get_object_or_404(service.orders(request.user),pk=pk)
    if order.kind=='repairs':raise Http404
    if not service.can_report(request.user,order) or accept and not request.user.is_leader:raise Http404
    latest=order.results.first();initial={'expected_version':order.version}
    if accept and latest:initial.update(outcome='complete',quantity=latest.quantity,date=latest.date)
    form=ResultForm(request.POST or None,request.FILES or None,initial=initial);status=200
    if request.method=='POST' and form.is_valid():
        try:service.submit_result(request.user,pk,form.cleaned_data,accept)
        except ValidationError as e:form.add_error(None,e);status=409
        else:return redirect('subcontract_order',pk=pk)
    return render(request,'projects/subcontract_form.html',{'form':form,'order':order,'title':'Исправить и принять' if accept else 'Внести выполнение','multipart':True,'result_form':True},status=status)

@leader_required
def decision(request,pk):
    order=get_object_or_404(service.orders(request.user),pk=pk);form=DecisionForm(request.POST or None,initial={'expected_version':order.version});status=200
    if request.method=='POST' and form.is_valid():
        c=form.cleaned_data
        try:service.decide(request.user,pk,c['expected_version'],c['decision'],c['note'])
        except ValidationError as e:form.add_error(None,e);status=409
        else:return redirect('subcontract_order',pk=pk)
    return render(request,'projects/subcontract_form.html',{'form':form,'order':order,'title':'Проверить выполнение'},status=status)

@login_required
def attachment(request,pk):
    row=get_object_or_404(SubcontractAttachment.objects.filter(result__order__in=service.orders(request.user,True)),pk=pk)
    try:stream=verified_file(row)
    except (OSError,ValueError):raise Http404
    inline=row.content_type in ('image/png','image/jpeg') and request.GET.get('preview')=='1'
    response=FileResponse(stream,as_attachment=not inline,filename=row.name,content_type=row.content_type if inline else 'application/octet-stream');response['Cache-Control']='private, no-store';response['Content-Security-Policy']="default-src 'none'; sandbox";return response


class UnlinkForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    note=forms.CharField(label='Причина снятия поручения',max_length=1500,widget=forms.Textarea(attrs={'rows':3}))

@permission_required('subcontract.manage')
def repair_unlink(request,pk,job_id):
    order=get_object_or_404(service.orders(request.user,True),pk=pk);job=get_object_or_404(visible_jobs(request.user),pk=job_id,subcontract_order=order)
    form=UnlinkForm(request.POST or None,initial={'expected_version':job.version});status=200
    if request.method=='POST' and form.is_valid():
        try:service.unlink_repair(request.user,pk,job_id,form.cleaned_data['expected_version'],form.cleaned_data['note'])
        except ValidationError as e:form.add_error(None,e);status=409
        else:return redirect('subcontract_order',pk=pk)
    return render(request,'projects/subcontract_form.html',{'form':form,'order':order,'title':'Снять поручение с ремонта'},status=status)
