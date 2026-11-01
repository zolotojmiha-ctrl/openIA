"""Customer-specific exchange; a scope grant never exposes all internal documents."""
from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404,render,redirect
from django.views.decorators.http import require_POST
from django.views.decorators.debug import sensitive_post_parameters
from .models import CustomerAccess,CustomerDocumentLink,Contract,ProjectObject,COMPANY_TAX_ID
from documents.models import Document,DocumentRead
from documents.access import visible_documents
from documents.storage import validate_upload, verified_file
from documents.services import make_version
from accounts.views import audit


def scope(user,contract_id,object_id):
    if not user.is_authenticated or not user.is_active:raise PermissionDenied
    if user.is_leader:
        contract=get_object_or_404(Contract,pk=contract_id,legal_entity__tax_id=COMPANY_TAX_ID)
        area=get_object_or_404(contract.objects_linked,pk=object_id)
    else:
        if user.role!='customer':raise PermissionDenied
        grant=get_object_or_404(CustomerAccess.objects.select_related('contract','project_object'),user=user,contract_id=contract_id,project_object_id=object_id)
        contract,area=grant.contract,grant.project_object
    return contract,area

class UploadForm(forms.Form):
    title=forms.CharField(label='Название документа',max_length=200,required=False)
    file=forms.FileField(label='ППР, совместный приказ или другой документ')
    def clean_file(self):return validate_upload(self.cleaned_data['file'])

class ShareForm(forms.Form):
    document=forms.ModelChoiceField(label='Открыть заказчику существующий файл',queryset=None)
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs);self.fields['document'].queryset=visible_documents(user).filter(archived=False,deleted_at__isnull=True,current_version__status__in=['available','approved'])

@login_required
@sensitive_post_parameters()
def listing(request,contract_id,object_id):
    contract,area=scope(request.user,contract_id,object_id)
    form=UploadForm(request.POST or None,request.FILES or None);share=ShareForm(request.POST or None,user=request.user) if request.user.is_leader else None
    action=request.POST.get('action','upload')
    if request.method=='POST':
        selected=share if action=='share' else form
        if action not in ('upload','share') or selected is None:raise PermissionDenied
        if selected.is_valid():
            with transaction.atomic():
                from accounts.models import User
                actor=User.objects.select_for_update().get(pk=request.user.pk)
                if not actor.is_active or actor.session_version!=request.user.session_version:raise PermissionDenied
                scope(actor,contract_id,object_id)
                if action=='share':
                    doc=selected.cleaned_data['document']
                else:
                    data=selected.cleaned_data['file']
                    doc=Document.objects.create(legal_entity=contract.legal_entity,title=selected.cleaned_data['title'] or data['name'],section='other',created_by=actor)
                    doc.objects_linked.add(area);doc.contracts_linked.add(contract)
                    make_version(doc,actor,data)
                row,_=CustomerDocumentLink.objects.update_or_create(contract=contract,project_object=area,document=doc,defaults={'created_by':actor,'active':True})
                audit(actor,'document_shared',kind='customer_exchange',document=str(doc.pk),contract=str(contract.pk),area=str(area.pk))
            return redirect('customer_documents',contract_id=contract_id,object_id=object_id)
    rows=CustomerDocumentLink.objects.filter(contract=contract,project_object=area,active=True,document__archived=False,document__deleted_at__isnull=True,document__current_version__status__in=['available','approved']).select_related('document__current_version','created_by')
    seen=dict(DocumentRead.objects.filter(user=request.user,document__in=rows.values('document_id')).values_list('document_id','version_id'))
    rows=list(rows)
    for row in rows:row.unread=row.document.created_by_id!=request.user.pk and seen.get(row.document_id)!=row.document.current_version_id
    return render(request,'projects/customer_documents.html',{'contract':contract,'area':area,'rows':rows,'form':form,'share_form':share})

@login_required
def download(request,pk):
    link=get_object_or_404(CustomerDocumentLink.objects.select_related('document__current_version'),pk=pk,active=True,document__archived=False,document__deleted_at__isnull=True,document__current_version__status__in=['available','approved'])
    scope(request.user,link.contract_id,link.project_object_id)
    version=link.document.current_version
    if not version:raise Http404
    stream=verified_file(version)
    DocumentRead.objects.update_or_create(document=link.document,user=request.user,defaults={'version':version})
    response=FileResponse(stream,as_attachment=True,filename=version.original_name,content_type='application/octet-stream');response['Cache-Control']='private, no-store';return response

@login_required
@require_POST
def revoke(request,pk):
    if not request.user.is_leader:raise PermissionDenied
    link=get_object_or_404(CustomerDocumentLink,pk=pk);scope(request.user,link.contract_id,link.project_object_id)
    link.active=False;link.save(update_fields=['active']);audit(request.user,'document_shared',kind='customer_exchange_revoked',link=str(pk))
    return redirect('customer_documents',contract_id=link.contract_id,object_id=link.project_object_id)

@login_required
def excel(request,contract_id,object_id):
    contract,area=scope(request.user,contract_id,object_id)
    from .models import CustomerPublication
    from .repair_materials import all_demand
    from repairs.excel import workbook
    from django.http import HttpResponse
    publication=CustomerPublication.objects.filter(contract=contract,project_object=area).first()
    rows=[]
    if publication and not publication.snapshot.get('withdrawn'):
        for d in publication.snapshot.get('rows',[]):rows.append(['Ход работ',d.get('pipeline',''),d.get('km',''),d.get('number',''),d.get('state',''),'','','',d.get('latitude',''),d.get('longitude','')])
        for d in all_demand():
            if d['job'].contract_id!=contract.pk or d['job'].section.project_object_id!=area.pk:continue
            label='; '.join('км '+x.km+' № '+x.number for x in d['job'].active_defects)
            for n in d['couplings']+d['materials']:
                if n['missing']>0:rows.append(['К поставке',d['job'].section.pipeline,label,'','',n['name'],str(n['missing']),n['unit'],'',''])
            if d['issues']:rows.append(['Потребность уточняется',d['job'].section.pipeline,label])
    data=workbook(rows,['Вид','Нефтепровод','Км / связанные дефекты','№ дефекта','Состояние','Материал','Необходимо поставить','Единица','Широта','Долгота'],'Кабинет заказчика')
    response=HttpResponse(data,content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');response['Content-Disposition']='attachment; filename="customer-works.xlsx"';response['Cache-Control']='private, no-store';return response

def unread_count(user,contract_id,object_id):
    from django.db.models import Exists,OuterRef
    read=DocumentRead.objects.filter(user=user,document_id=OuterRef('document_id'),version_id=OuterRef('document__current_version_id'))
    return CustomerDocumentLink.objects.filter(contract_id=contract_id,project_object_id=object_id,active=True,document__archived=False,document__deleted_at__isnull=True,document__current_version__status__in=['available','approved']).exclude(document__created_by=user).annotate(seen=Exists(read)).filter(seen=False).count()
