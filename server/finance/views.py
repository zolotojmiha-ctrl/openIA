from functools import wraps
from uuid import UUID
from django.urls import reverse
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction,IntegrityError
from django.http import HttpResponse
from django.shortcuts import render,redirect,get_object_or_404
from django.utils import timezone
from accounts.models import User
from accounts.views import audit
from documents.access import has_permission
from projects.models import LegalEntity,COMPANY_TAX_ID
from .models import FinancialOperation,FinancialScope,FinancialImport,OpeningSnapshot,PendingWork,CashReceipt,PaymentAllocation,WorkExecution,AdvanceOffset,CostEntry,SubcontractObligation,PriceChange
from .forms import ScopeForm,ReceiptForm,WorkForm,AcceptanceForm,OffsetForm,AllocationForm,CostForm,SubcontractForm,PriceForm,ImportForm
from .services import summary,total,ZERO
from .importer import parse_report


def allowed(user,write=False):return has_permission(user,'finance.write' if write else 'finance.read') or (not write and has_permission(user,'finance.write'))

def finance_required(write=False):
    def decorate(view):
        @login_required
        @wraps(view)
        def wrapped(request,*args,**kwargs):
            if not allowed(request.user,write):raise PermissionDenied
            return view(request,*args,**kwargs)
        return wrapped
    return decorate


def previous_operation(request):
    try:token=UUID(request.POST.get('operation_token',''))
    except ValueError:return None
    return FinancialOperation.objects.filter(user=request.user,token=token).first()

def record_operation(user,form,path):
    if form.cleaned_data.get('operation_token'):FinancialOperation.objects.create(user=user,token=form.cleaned_data['operation_token'],result_path=path)


def scopes():return FinancialScope.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID).select_related('opening','contract')

def dashboard_data(user):
    cards=[summary(s) for s in scopes().order_by('title')]
    for card in cards:
        source=card.get('base')
        doc=visible_source(user,card['scope'])
        card['source_label']=source.source.original_name if source and doc else 'Подтверждённая начальная справка' if source else ''
    aggregate={key:sum((card[key] for card in cards),ZERO) for key in ['advances','paid','offsets','accepted','pending','reported_advance_balance','expected_advance_balance','advance_difference','direct','overhead','subcontract_remaining','due_future','due_overdue','due_unknown']}
    aggregate['undistributed']=total(CashReceipt.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID))-total(PaymentAllocation.objects.filter(scope__legal_entity__tax_id=COMPANY_TAX_ID))
    return {'finance_cards':cards,'finance_totals':aggregate}

@finance_required()
def dashboard(request):
    context=dashboard_data(request.user);context['can_write']=allowed(request.user,True)
    context['receipts']=CashReceipt.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID).prefetch_related('allocations').order_by('-date','-created_at')[:100]
    context['common_costs']=CostEntry.objects.filter(scope__isnull=True,legal_entity__tax_id=COMPANY_TAX_ID).order_by('-date')[:100]
    return render(request,'finance/dashboard.html',context)

@finance_required()
def detail(request,pk):
    scope=get_object_or_404(scopes(),pk=pk)
    return render(request,'finance/detail.html',{'card':summary(scope),'scope':scope,'can_write':allowed(request.user,True),'works':scope.executions.select_related('accepted_by').order_by('-date'),'allocations':scope.allocations.select_related('receipt','work').order_by('-created_at'),'costs':scope.costs.order_by('-date'),'subcontracts':scope.subcontracts.all(),'offsets':AdvanceOffset.objects.filter(work__scope=scope).select_related('work').order_by('-date'),'source_document':visible_source(request.user,scope),'source_version':scope.opening.source.source_version if getattr(scope,'opening',None) else None,'pending':scope.pending_work.all()})

@finance_required(True)
def create(request,kind):
    if request.method=='POST':
        prior=previous_operation(request)
        if prior:return redirect(prior.result_path)
    forms={'scope':ScopeForm,'receipt':ReceiptForm,'work':WorkForm,'offset':OffsetForm,'cost':CostForm,'subcontract':SubcontractForm,'price':PriceForm}
    titles={'scope':'Направление финансового учёта','receipt':'Полученная выплата','work':'Фактическое выполнение','offset':'Ручной зачёт аванса','cost':'Фактические затраты','subcontract':'Обязательство субподрядчику','price':'Изменение цены договора'}
    if kind not in forms:raise PermissionDenied
    initial={}
    if request.GET.get('scope'):
        from uuid import UUID
        try:scope=get_object_or_404(scopes(),pk=UUID(request.GET['scope']));initial['scope']=scope.pk
        except ValueError:raise PermissionDenied
    if kind=='work' and request.GET.get('pending','').isdigit():
        pending=get_object_or_404(PendingWork,pk=request.GET['pending'],scope__legal_entity__tax_id=COMPANY_TAX_ID)
        initial.update(scope=pending.scope_id,title=pending.title,factual_amount=pending.amount,source_pending=pending.pk)
    form=forms[kind](request.POST or None,initial=initial)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            user=User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
            if not allowed(user,True):raise PermissionDenied
            prior=previous_operation(request)
            if prior:return redirect(prior.result_path)
            saved=form.save(commit=False);saved.created_by=user
            if kind in ('scope','receipt','cost'):saved.legal_entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
            if kind=='receipt':
                LegalEntity.objects.select_for_update().get(tax_id=COMPANY_TAX_ID)
                duplicate=CashReceipt.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID,date=saved.date,source=saved.source,reference=saved.reference,amount=saved.amount).exists()
                if duplicate and not form.cleaned_data.get('confirm_duplicate'):form.add_error('confirm_duplicate','Такая выплата уже внесена. Проверьте повтор.')
                prior=CashReceipt.objects.filter(token=saved.token).first()
                if prior:return redirect('finance_allocate',pk=prior.pk)
            if kind=='work':
                FinancialScope.objects.select_for_update().get(pk=saved.scope_id)
                if saved.source_pending_id:
                    PendingWork.objects.select_for_update().get(pk=saved.source_pending_id)
                    if WorkExecution.objects.filter(source_pending_id=saved.source_pending_id).exists():form.add_error('source_pending','Эта строка справки уже перенесена в текущий учёт.')
            if kind=='offset':
                work=WorkExecution.objects.get(pk=saved.work_id)
                scope=FinancialScope.objects.select_for_update().get(pk=work.scope_id)
                work=WorkExecution.objects.select_for_update().get(pk=work.pk)
                card=summary(scope)
                if saved.amount>card['expected_advance_balance']:form.add_error('amount','Зачёт больше расчётного незачтённого аванса.')
                if saved.amount>work.accepted_amount-total(work.offsets)-total(work.payments):form.add_error('amount','Зачёт превышает неоплаченное принятое выполнение.')
            if kind=='price':
                contract=type(saved.contract).objects.select_for_update().get(pk=saved.contract_id)
                if contract.amount is None:form.add_error('contract','Сначала укажите первоначальную цену договора.')
                elif contract.amount+total(contract.price_changes)+saved.amount<0:form.add_error('amount','Итоговая цена не может стать отрицательной.')
            if not form.errors:
                saved.save();audit(user,'finance_created',entity_type=kind,entity_id=str(saved.pk))
                path=reverse('finance_allocate',args=[saved.pk]) if kind=='receipt' else reverse('finance_detail',args=[saved.work.scope_id]) if kind=='offset' else reverse('finance_detail',args=[saved.scope_id]) if getattr(saved,'scope_id',None) else reverse('finance')
                record_operation(user,form,path)
                return redirect(path)
    return render(request,'finance/form.html',{'form':form,'title':titles[kind],'kind':kind})

@finance_required(True)
def allocate(request,pk):
    if request.method=='POST':
        prior=previous_operation(request)
        if prior:return redirect(prior.result_path)
    receipt=get_object_or_404(CashReceipt,pk=pk,legal_entity__tax_id=COMPANY_TAX_ID)
    form=AllocationForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            user=User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
            if not allowed(user,True):raise PermissionDenied
            prior=previous_operation(request)
            if prior:return redirect(prior.result_path)
            locked=CashReceipt.objects.select_for_update().get(pk=pk)
            item=form.save(commit=False)
            scope=FinancialScope.objects.select_for_update().get(pk=item.scope_id)
            if item.purpose=='work' and item.amount>summary(scope)['accepted_unpaid']:form.add_error('amount','Оплата превышает непогашенное принятое выполнение направления.')
            if item.amount>locked.amount-total(locked.allocations):form.add_error('amount','Распределение превышает остаток платежа.')
            if item.purpose=='work' and not item.work_id:
                base=getattr(scope,'opening',None)
                initial_debt=(base.accepted-base.paid_for_work-base.advance_offset) if base else ZERO
                if item.amount>initial_debt-total(scope.allocations.filter(purpose='work',work__isnull=True)):form.add_error('work','Выберите принятое выполнение, за которое пришла оплата.')
            if item.work_id:
                work=WorkExecution.objects.select_for_update().get(pk=item.work_id)
                if item.amount>work.accepted_amount-total(work.payments)-total(work.offsets):form.add_error('amount','Оплата превышает непогашенное принятое выполнение.')
            if not form.errors:
                item.receipt=locked;item.created_by=user;item.save();audit(user,'finance_allocated',receipt_id=str(pk),allocation_id=item.pk)
                path=reverse('finance_allocate',args=[pk]);record_operation(user,form,path);return redirect(path)
    return render(request,'finance/allocate.html',{'receipt':receipt,'remaining':receipt.amount-total(receipt.allocations),'form':form,'allocations':receipt.allocations.select_related('scope','work')})

@finance_required()
def accept(request,pk):
    if not request.user.is_leader:raise PermissionDenied
    work=get_object_or_404(WorkExecution,pk=pk,scope__legal_entity__tax_id=COMPANY_TAX_ID)
    form=AcceptanceForm(request.POST or None,initial={'expected_revision':work.revision})
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            FinancialScope.objects.select_for_update().get(pk=work.scope_id)
            work=WorkExecution.objects.select_for_update().get(pk=pk)
            if work.accepted_by_id or work.revision!=form.cleaned_data['expected_revision']:return HttpResponse('Выполнение уже подтверждено или изменено.',status=409)
            if form.cleaned_data['accepted_amount']>work.factual_amount:form.add_error('accepted_amount','Принято больше фактического выполнения. Сначала уточните объём.')
            else:
                work.accepted_amount=form.cleaned_data['accepted_amount'];work.payment_due=form.cleaned_data['payment_due'];work.basis=form.cleaned_data['basis'];work.accepted_by=request.user;work.accepted_at=timezone.now();work.revision+=1
                work.save();audit(request.user,'finance_accepted',entity_id=str(pk));return redirect('finance_detail',pk=work.scope_id)
    return render(request,'finance/form.html',{'form':form,'title':'Подтвердить принятое выполнение · '+work.title})

def save_report(user,report,original_name,source_document=None,source_version=None):
    with transaction.atomic():
        user=User.objects.select_for_update(no_key=True).get(pk=user.pk)
        if not user.is_active or not user.is_leader:raise PermissionDenied
        LegalEntity.objects.select_for_update().get(tax_id=COMPANY_TAX_ID)
        if FinancialImport.objects.filter(sha256=report['sha256']).exists():return 'Эта справка уже загружена. Повторный ввод не выполнен.'
        if scopes().filter(title__in=[b['title'] for b in report['blocks']]).exists():return 'Направление с таким названием уже существует. Новая полная справка не добавляется поверх старой, чтобы не удвоить показатели. Используйте записи последующих операций.'
        source=FinancialImport.objects.create(sha256=report['sha256'],original_name=original_name[:200],uploaded_by=user,source_document=source_document,source_version=source_version)
        for block in report['blocks']:
            scope=FinancialScope.objects.create(title=block['title'],legal_entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID),created_by=user)
            fields={key:block[key] for key in ['total_defects','repaired_defects','advance_received','accepted','paid_for_work','advance_offset','declared_advance_balance','declared_pending']}
            OpeningSnapshot.objects.create(scope=scope,source=source,**fields)
            PendingWork.objects.bulk_create([PendingWork(scope=scope,title=line['title'],amount=line['amount'],stage=line['title'],source=source) for line in block['lines']])
        audit(user,'finance_imported',source_id=source.pk,sha256=source.sha256,scopes=len(report['blocks']))
    return None

@finance_required(True)
def import_report(request):
    from .models import FinancialDraft
    form=ImportForm(request.POST or None,request.FILES or None);report=None;draft=None
    if request.method=='POST' and form.is_valid():
        try:report=parse_report(form.cleaned_data['file'])
        except ValidationError as e:form.add_error('file',e)
        if report and FinancialImport.objects.filter(sha256=report['sha256']).exists():form.add_error(None,'Эта справка уже загружена. Повторный ввод не выполнен.')
        if report and not form.errors:
            if form.cleaned_data.get('confirm'):
                if not request.user.is_leader:form.add_error('confirm','Начальные показатели принятого выполнения подтверждает один из двух руководителей.')
                else:
                    error=save_report(request.user,report,form.cleaned_data['file'].name)
                    if error:form.add_error(None,error)
                    else:return redirect('finance')
            if not form.errors:
                from datetime import timedelta
                FinancialDraft.objects.filter(user=request.user,created_at__lt=timezone.now()-timedelta(days=1)).delete()
                existing=FinancialDraft.objects.filter(user=request.user,payload__sha256=report['sha256']).first()
                if existing:draft=existing
                else:
                    from documents.models import Document
                    from documents.storage import validate_upload,store_file,private_path
                    from documents.services import make_version
                    data=validate_upload(form.cleaned_data['file']);key=None
                    try:
                        with transaction.atomic():
                            key=store_file(data)
                            doc=Document.objects.create(title=data['name'],section='general',created_by=request.user,legal_entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID),link_not_required=True)
                            version=make_version(doc,request.user,data,key=key)
                            draft=FinancialDraft.objects.create(user=request.user,original_name=data['name'],payload=report,source_document=doc,source_version=version)
                    except Exception:
                        if key:private_path(key).unlink(missing_ok=True)
                        raise
    return render(request,'finance/import.html',{'form':form,'report':report,'draft':draft})

@finance_required(True)
def confirm_report(request,pk):
    from datetime import timedelta
    from .models import FinancialDraft
    if not request.user.is_leader:raise PermissionDenied
    draft=get_object_or_404(FinancialDraft,pk=pk,user=request.user,created_at__gte=timezone.now()-timedelta(days=1))
    if request.method!='POST':return redirect('finance_import')
    error=save_report(request.user,draft.payload,draft.original_name,draft.source_document,draft.source_version)
    if error:messages.error(request,error);return redirect('finance_import')
    draft.delete();messages.success(request,'Показатели справки сохранены. Расхождения источника отмечены для сверки.')
    return redirect('finance')

@finance_required(True)
def scope_edit(request,pk):
    scope=get_object_or_404(scopes(),pk=pk)
    form=ScopeForm(request.POST or None,instance=scope,initial={'expected_revision':scope.revision})
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            user=User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
            if not allowed(user,True):raise PermissionDenied
            locked=FinancialScope.objects.select_for_update().get(pk=pk)
            if form.cleaned_data['expected_revision']!=locked.revision:form.add_error(None,'Карточка изменена. Обновите страницу.')
            else:
                saved=form.save(commit=False);saved.revision=locked.revision+1;saved.save()
                audit(user,'finance_scope_updated',entity_id=str(pk),previous_contract=str(locked.contract_id or ''),contract=str(saved.contract_id or ''))
                return redirect('finance_detail',pk=pk)
    return render(request,'finance/form.html',{'form':form,'title':'Параметры финансового направления'})


@finance_required(True)
def reverse_allocation(request,pk):
    from django import forms
    class ReversalForm(forms.Form):
        reason=forms.CharField(label='Почему отменяется распределение',max_length=500,widget=forms.Textarea)
    allocation=get_object_or_404(PaymentAllocation,pk=pk,scope__legal_entity__tax_id=COMPANY_TAX_ID)
    form=ReversalForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            user=User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
            if not allowed(user,True):raise PermissionDenied
            CashReceipt.objects.select_for_update().get(pk=allocation.receipt_id)
            FinancialScope.objects.select_for_update().get(pk=allocation.scope_id)
            allocation=PaymentAllocation.objects.select_for_update().get(pk=pk)
            if allocation.reversed:return HttpResponse('Распределение уже отменено.',status=409)
            # An advance already consumed by offsets cannot be withdrawn underneath them.
            if allocation.purpose=='advance' and summary(allocation.scope)['expected_advance_balance']<allocation.amount:
                form.add_error(None,'Аванс уже использован в зачётах. Сначала нужно сверить и исправить связанные зачёты с руководителем.')
            else:
                allocation.reversed=True;allocation.reversal_reason=form.cleaned_data['reason'];allocation.save(update_fields=['reversed','reversal_reason'])
                audit(user,'finance_allocation_reversed',allocation_id=pk,reason=allocation.reversal_reason)
                return redirect('finance_allocate',pk=allocation.receipt_id)
    return render(request,'finance/form.html',{'form':form,'title':'Отменить ошибочное распределение'})

@finance_required(True)
def edit_entry(request,kind,pk):
    from django import forms
    from django.forms.models import model_to_dict
    from django.utils.crypto import salted_hmac,constant_time_compare
    choices={'receipt':(CashReceipt,ReceiptForm),'cost':(CostEntry,CostForm),'subcontract':(SubcontractObligation,SubcontractForm),'price':(PriceChange,PriceForm)}
    if kind not in choices or not request.user.is_leader:raise PermissionDenied
    model,form_class=choices[kind]
    qs=model.objects.all()
    qs=qs.filter(contract__legal_entity__tax_id=COMPANY_TAX_ID) if kind=='price' else qs.filter(scope__legal_entity__tax_id=COMPANY_TAX_ID) if kind=='subcontract' else qs.filter(legal_entity__tax_id=COMPANY_TAX_ID)
    from django.http import Http404
    try:pk=model._meta.pk.to_python(pk)
    except (ValidationError,ValueError):raise Http404
    obj=get_object_or_404(qs,pk=pk)
    def state(item):return {key:str(value) for key,value in model_to_dict(item).items()}
    def signature(item):return salted_hmac('finance-edit',str(sorted(state(item).items()))).hexdigest()
    initial={'previous':signature(obj)}
    form=form_class(request.POST or None,instance=obj,initial=initial)
    form.fields['previous']=forms.CharField(widget=forms.HiddenInput)
    form.fields['reason']=forms.CharField(label='Причина исправления',max_length=500)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            locked=get_object_or_404(qs.select_for_update(),pk=pk)
            before=state(locked)
            if not constant_time_compare(signature(locked),form.cleaned_data['previous']):form.add_error(None,'Запись уже изменилась. Обновите страницу.')
            else:
                saved=form.save(commit=False)
                if kind=='receipt' and saved.amount<total(locked.allocations):form.add_error('amount','Сумма ниже уже распределённого платежа.')
                if kind=='price' and saved.contract_id!=locked.contract_id:form.add_error('contract','Изменение цены нельзя перенести на другой договор.')
                if kind=='price' and saved.contract.amount is not None and saved.contract.amount+total(saved.contract.price_changes.exclude(pk=pk))+saved.amount<0:form.add_error('amount','Итоговая цена не может стать отрицательной.')
                if not form.errors:
                    saved.save();audit(request.user,'finance_corrected',entity_type=kind,entity_id=str(pk),reason=form.cleaned_data['reason'],before=before,after=state(saved))
                    return redirect('finance')
    return render(request,'finance/form.html',{'form':form,'title':'Исправить финансовую запись с сохранением истории'})


@finance_required(True)
def reverse_offset(request,pk):
    from django import forms
    class OffsetReversalForm(forms.Form):
        reason=forms.CharField(label='Причина отмены зачёта',max_length=500,widget=forms.Textarea)
    offset=get_object_or_404(AdvanceOffset,pk=pk,work__scope__legal_entity__tax_id=COMPANY_TAX_ID)
    form=OffsetReversalForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            user=User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
            if not allowed(user,True):raise PermissionDenied
            FinancialScope.objects.select_for_update().get(pk=offset.work.scope_id)
            WorkExecution.objects.select_for_update().get(pk=offset.work_id)
            offset=AdvanceOffset.objects.select_for_update().get(pk=pk)
            if offset.reversed:return HttpResponse('Зачёт уже отменён.',status=409)
            offset.reversed=True;offset.reversal_reason=form.cleaned_data['reason'];offset.save(update_fields=['reversed','reversal_reason'])
            audit(user,'finance_offset_reversed',offset_id=pk,reason=offset.reversal_reason)
            return redirect('finance_detail',pk=offset.work.scope_id)
    return render(request,'finance/form.html',{'form':form,'title':'Отменить ошибочный зачёт'})


def visible_source(user,scope):
    source=getattr(scope,'opening',None)
    if not source or not source.source.source_document_id:return None
    from documents.access import visible_documents
    return visible_documents(user).filter(pk=source.source.source_document_id).first()
