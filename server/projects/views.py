from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from uuid import UUID
from django.shortcuts import get_object_or_404, redirect, render
from accounts.models import User
from accounts.views import leader_required, audit
from .access import visible_objects, visible_contracts, contract_card
from .forms import ObjectForm, ContractForm, AssignmentForm, ContractShareForm
from .models import LegalEntity, ProjectObject, Contract, ContractObject, ObjectAccess, ContractAccess, COMPANY_TAX_ID, SHARED_FIELDS

@login_required
def objects(request):
    q=request.GET.get('q','').strip()[:200]
    qs=visible_objects(request.user).order_by('name','id')
    if q:
        qs=qs.filter(Q(name__icontains=q)|Q(location__icontains=q)|Q(kind__icontains=q))
    return render(request,'projects/objects.html',{'page':Paginator(qs,25).get_page(request.GET.get('page')),'q':q})

@login_required
def object_detail(request,pk):
    obj=get_object_or_404(visible_objects(request.user),pk=pk)
    contracts=visible_contracts(request.user).filter(objects_linked=obj).prefetch_related('access_rules')
    return render(request,'projects/object.html',{'object':obj,'cards':[contract_card(c,request.user) for c in contracts],
        'assignments':obj.assignments.select_related('user').order_by('user__full_name') if request.user.is_leader else None})

@leader_required
def object_edit(request,pk=None):
    obj=get_object_or_404(visible_objects(request.user),pk=pk) if pk else None
    form=ObjectForm(request.POST or None,instance=obj,initial={'expected_version':obj.version} if obj else None)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            if obj:
                locked=ProjectObject.objects.select_for_update().get(pk=obj.pk)
                if locked.version!=form.cleaned_data['expected_version']:
                    form.add_error(None,'Объект уже изменён. Обновите страницу и проверьте изменения.')
                else:
                    saved=form.save(commit=False)
                    saved.version=locked.version+1
            else:
                saved=form.save(commit=False)
                saved.legal_entity=get_object_or_404(LegalEntity,tax_id=COMPANY_TAX_ID)
            if not form.errors:
                saved.save()
                audit(request.user,'object_updated' if obj else 'object_created',entity_id=str(saved.pk),summary=saved.name)
                messages.success(request,'Объект сохранён.')
                return redirect('object_detail',pk=saved.pk)
    return render(request,'projects/edit.html',{'form':form,'title':'Редактирование объекта' if obj else 'Новый объект','back':'objects'})

@leader_required
def assignments(request,pk):
    obj=get_object_or_404(visible_objects(request.user),pk=pk)
    form=AssignmentForm(request.POST or None,initial={'expected_version':obj.version,'employees':list(obj.assignments.values_list('user_id',flat=True))})
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            locked=ProjectObject.objects.select_for_update().get(pk=obj.pk)
            if locked.version!=form.cleaned_data['expected_version']:
                form.add_error(None,'Назначения или объект уже изменились. Обновите страницу.')
            else:
                desired={u.pk for u in form.cleaned_data['employees']}
                existing=set(obj.assignments.values_list('user_id',flat=True))
                affected=desired.symmetric_difference(existing)
                # Serialize access changes with employee edits and revoke stale sessions.
                users=list(User.objects.select_for_update().filter(pk__in=affected).order_by('pk'))
                obj.assignments.exclude(user_id__in=desired).delete()
                ObjectAccess.objects.bulk_create([ObjectAccess(project_object=obj,user_id=uid,granted_by=request.user) for uid in desired-existing])
                for employee in users:
                    remaining=Contract.objects.filter(objects_linked__assignments__user=employee).values('pk')
                    employee.contract_access.exclude(contract_id__in=remaining).delete()
                    employee.session_version+=1
                    employee.save(update_fields=['session_version'])
                locked.version+=1
                locked.save(update_fields=['version','updated_at'])
                audit(request.user,'object_access_changed',entity_id=str(obj.pk),summary=obj.name,assigned=[str(uid) for uid in desired],removed=[str(uid) for uid in existing-desired])
                messages.success(request,'Назначения сохранены. Сеансы сотрудников с изменённым доступом завершены.')
                return redirect('object_detail',pk=obj.pk)
    return render(request,'projects/assignments.html',{'object':obj,'form':form})

@login_required
def contracts(request):
    q=request.GET.get('q','').strip()[:200]
    qs=visible_contracts(request.user)
    if q:
        if request.user.is_leader:
            qs=qs.filter(Q(number__icontains=q)|Q(subject__icontains=q)|Q(counterparty__icontains=q))
        else:
            # The SAME employee rule gates each search term, not just the rendered fields.
            qs=qs.filter(Q(access_rules__user=request.user)&(
                Q(access_rules__show_number=True,number__icontains=q)|
                Q(access_rules__show_subject=True,subject__icontains=q)|
                Q(access_rules__show_counterparty=True,counterparty__icontains=q)))
    page=Paginator(qs.order_by('id').prefetch_related('access_rules'),25).get_page(request.GET.get('page'))
    return render(request,'projects/contracts.html',{'cards':[contract_card(c,request.user) for c in page],'page':page,'q':q})

@login_required
def contract_detail(request,pk):
    contract=get_object_or_404(visible_contracts(request.user),pk=pk)
    linked=visible_objects(request.user).filter(contracts=contract).order_by('name')
    shares=None
    if request.user.is_leader:
        shares=[{'user':rule.user,'fields':[label for flag,label in SHARED_FIELDS.items() if getattr(rule,flag)]} for rule in contract.access_rules.select_related('user').order_by('user__full_name')]
    return render(request,'projects/contract.html',{'card':contract_card(contract,request.user),'linked':linked,'shares':shares})

@leader_required
def contract_edit(request,pk=None):
    obj=get_object_or_404(visible_contracts(request.user),pk=pk) if pk else None
    initial={'expected_version':obj.version,'linked_objects':obj.objects_linked.all()} if obj else None
    form=ContractForm(request.POST or None,instance=obj,initial=initial)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            if obj:
                locked=Contract.objects.select_for_update().get(pk=obj.pk)
                if locked.version!=form.cleaned_data['expected_version']:
                    form.add_error(None,'Договор уже изменён. Обновите страницу и проверьте изменения.')
                else:
                    saved=form.save(commit=False)
                    saved.version=locked.version+1
            else:
                saved=form.save(commit=False)
                saved.legal_entity=get_object_or_404(LegalEntity,tax_id=COMPANY_TAX_ID)
            if not form.errors:
                saved.save()
                desired={o.pk for o in form.cleaned_data['linked_objects']}
                previous=set(saved.objects_linked.values_list('pk',flat=True))
                ContractObject.objects.filter(contract=saved).exclude(project_object_id__in=desired).delete()
                for oid in desired-previous:
                    ContractObject.objects.create(contract=saved,project_object_id=oid)
                for rule in saved.access_rules.select_related('user'):
                    if not ObjectAccess.objects.filter(user=rule.user,project_object__contracts=saved).exists():
                        rule.delete()
                audit(request.user,'contract_updated' if obj else 'contract_created',entity_id=str(saved.pk),summary=saved.number,linked=[str(oid) for oid in desired],unlinked=[str(oid) for oid in previous-desired])
                messages.success(request,'Договор сохранён.')
                return redirect('contract_detail',pk=saved.pk)
    return render(request,'projects/edit.html',{'form':form,'title':'Редактирование договора' if obj else 'Новый договор','back':'contracts'})

@leader_required
def share_contract(request,pk):
    contract=get_object_or_404(visible_contracts(request.user),pk=pk)
    initial={'expected_version':contract.version}
    selected=request.GET.get('employee')
    if selected:
        try:
            selected=UUID(selected)
        except (ValueError,TypeError):
            selected=None
        rule=contract.access_rules.filter(user_id=selected).first() if selected else None
        if selected:
            initial['employee']=selected
        if rule:
            initial.update(employee=rule.user_id,enabled=True,fields=[k for k in SHARED_FIELDS if getattr(rule,k)])
    form=ContractShareForm(request.POST or None,contract=contract,initial=initial)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            locked=Contract.objects.select_for_update().get(pk=contract.pk)
            if locked.version!=form.cleaned_data['expected_version']:
                form.add_error(None,'Договор или доступ уже изменились. Обновите страницу.')
            else:
                employee=User.objects.select_for_update().get(pk=form.cleaned_data['employee'].pk)
                if not ObjectAccess.objects.filter(user=employee,project_object__contracts=locked).exists():
                    form.add_error(None,'Сотруднику больше не назначен ни один объект этого договора. Обновите страницу.')
                    return render(request,'projects/share.html',{'card':contract_card(contract,request.user),'form':form})
                if form.cleaned_data['enabled']:
                    ContractAccess.objects.update_or_create(contract=contract,user=employee,defaults={'granted_by':request.user,**{key:key in form.cleaned_data['fields'] for key in SHARED_FIELDS}})
                else:
                    contract.access_rules.filter(user=employee).delete()
                employee.session_version+=1
                employee.save(update_fields=['session_version'])
                locked.version+=1
                locked.save(update_fields=['version','updated_at'])
                audit(request.user,'contract_access_changed',employee,entity_id=str(contract.pk),summary=contract.number,shared_fields=form.cleaned_data['fields'] if form.cleaned_data['enabled'] else [])
                messages.success(request,'Доступ к договору сохранён. Прежние сеансы сотрудника завершены.')
                return redirect('contract_detail',pk=contract.pk)
    return render(request,'projects/share.html',{'card':contract_card(contract,request.user),'form':form})
