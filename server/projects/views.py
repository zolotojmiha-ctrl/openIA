from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from uuid import UUID
from django.shortcuts import get_object_or_404, redirect, render
from accounts.models import User
from accounts.permissions import permission_required,permitted
from django.core.exceptions import PermissionDenied
from documents.access import visible_documents,visible_people
from documents.presentation import file_groups
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
    from .equipment import placements,current
    equipment_here=current(placements(request.user)).filter(project_object=obj).order_by('equipment__name')
    from .stock import warehouses
    stock_here=warehouses(request.user).filter(project_object=obj,archived=False)
    from .subcontracts import orders
    subcontract_here=orders(request.user).filter(project_object=obj)
    return render(request,'projects/object.html',{'subcontract_here':subcontract_here,'stock_here':stock_here,'equipment_here':equipment_here,'documents':visible_documents(request.user).filter(objects_linked=obj,archived=False),'file_groups':file_groups(request.user,visible_documents(request.user).filter(objects_linked=obj,archived=False)),'personnel':visible_people(request.user).filter(assignments__project_object=obj).distinct(),'object':obj,'cards':[contract_card(c,request.user) for c in contracts],
        'assignments':obj.assignments.select_related('user').order_by('user__full_name') if permitted(request.user,'projects.access') else None})

@permission_required('projects.manage')
def object_edit(request,pk=None):
    obj=get_object_or_404(visible_objects(request.user),pk=pk) if pk else None
    form=ObjectForm(request.POST or None,instance=obj,initial={'expected_version':obj.version} if obj else None)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            actor=User.objects.select_for_update().get(pk=request.user.pk)
            if not permitted(actor,'projects.manage'):raise PermissionDenied
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
                if not obj and not request.user.is_leader:ObjectAccess.objects.create(user=request.user,project_object=saved,granted_by=request.user)
                audit(request.user,'object_updated' if obj else 'object_created',entity_id=str(saved.pk),summary=saved.name)
                messages.success(request,'Объект сохранён.')
                return redirect('object_detail',pk=saved.pk)
    return render(request,'projects/edit.html',{'form':form,'title':'Редактирование объекта' if obj else 'Новый объект','back':'objects'})

@permission_required('projects.access')
def assignments(request,pk):
    obj=get_object_or_404(visible_objects(request.user),pk=pk)
    form=AssignmentForm(request.POST or None,initial={'expected_version':obj.version,'employees':list(obj.assignments.values_list('user_id',flat=True))})
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            actor=User.objects.get(pk=request.user.pk)
            if not permitted(actor,'projects.access') or not visible_objects(actor).filter(pk=obj.pk).exists():raise PermissionDenied
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
    archived=request.GET.get('archive')=='1'
    archive_count=visible_contracts(request.user).filter(status='archived').count()
    q=request.GET.get('q','').strip()[:200]
    qs=visible_contracts(request.user)
    qs=qs.filter(status='archived') if archived else qs.exclude(status='archived')
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
    pending=[]
    if request.user.is_leader:
        from .closure import pending_contracts
        pending=[contract_card(c,request.user) for c in pending_contracts(request.user)]
    return render(request,'projects/contracts.html',{'archive_count':archive_count,'archived_contracts':archived,'pending_closures':pending,'cards':[contract_card(c,request.user) for c in page],'page':page,'q':q})

@login_required
def contract_detail(request,pk):
    contract=get_object_or_404(visible_contracts(request.user),pk=pk)
    linked=visible_objects(request.user).filter(contracts=contract).order_by('name')
    shares=None
    if request.user.is_leader:
        shares=[{'user':rule.user,'fields':[label for flag,label in SHARED_FIELDS.items() if getattr(rule,flag)]} for rule in contract.access_rules.select_related('user').order_by('user__full_name')]
    finance_context={}
    from finance.views import allowed,dashboard_data
    if allowed(request.user):
        finance_context=dashboard_data(request.user)
        finance_context['finance_cards']=[c for c in finance_context['finance_cards'] if c['scope'].contract_id==contract.pk]
        finance_context.pop('finance_totals',None)
    return render(request,'projects/contract.html',{'documents':visible_documents(request.user).filter(contracts_linked=contract,archived=False),'file_groups':file_groups(request.user,visible_documents(request.user).filter(contracts_linked=contract,archived=False)),'card':contract_card(contract,request.user),'linked':linked,'shares':shares,'closure_events':contract.closure_events.select_related('actor') if permitted(request.user,'projects.manage') else contract.closure_events.none(),'closure_pending':contract.closure_events.first() and contract.closure_events.first().action=='request','contract_status':contract.status,'contract_version':contract.version,**finance_context})

@permission_required('projects.manage')
def contract_edit(request,pk=None):
    obj=get_object_or_404(visible_contracts(request.user),pk=pk) if pk else None
    if obj and obj.status=='archived':
        from .archive import MESSAGE
        raise PermissionDenied(MESSAGE)
    if obj and not request.user.is_leader and obj.objects_linked.exclude(pk__in=visible_objects(request.user)).exists():raise PermissionDenied
    initial={'expected_version':obj.version,'linked_objects':obj.objects_linked.all()} if obj else None
    form=ContractForm(request.POST or None,instance=obj,initial=initial,user=request.user)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            actor=User.objects.select_for_update().get(pk=request.user.pk)
            if not permitted(actor,'projects.manage'):raise PermissionDenied
            locked=get_object_or_404(Contract.objects.select_for_update().filter(pk__in=visible_contracts(actor).values('pk')),pk=obj.pk) if obj else None
            if locked and locked.version!=form.cleaned_data['expected_version']:
                form.add_error(None,'Договор уже изменён. Обновите страницу и проверьте изменения.')
            desired={o.pk for o in form.cleaned_data['linked_objects']}
            if desired-set(visible_objects(actor).values_list('pk',flat=True)):raise PermissionDenied
            saved=form.instance
            from repairs.models import Job
            if locked and (Job.objects.filter(contract=locked).exclude(section__project_object_id__in=desired).exists() or (Job.objects.filter(contract=locked).exists() and 'repairs' not in saved.work_kinds)):
                form.add_error(None,'Нельзя убрать место или вид работ, пока с ними связаны ремонтные задания.')
            if locked:
                for related in (locked.daily_works,locked.brigade_assignments,locked.equipment_placements,locked.subcontract_orders):
                    if related.exclude(project_object_id__in=desired).exists():form.add_error(None,'Нельзя убрать объект с выполнениями или назначениями.')
                if locked.daily_works.exclude(kind__in=saved.work_kinds).exists():form.add_error(None,'Нельзя убрать вид работ с ежедневными выполнениями.')
            if not form.errors:
                saved.version=locked.version+1 if locked else 1
                if not obj:saved.legal_entity=get_object_or_404(LegalEntity,tax_id=COMPANY_TAX_ID)
                saved.save()
                previous=set(saved.objects_linked.values_list('pk',flat=True))
                ContractObject.objects.filter(contract=saved).exclude(project_object_id__in=desired).delete()
                for oid in desired-previous:ContractObject.objects.create(contract=saved,project_object_id=oid)
                for rule in saved.access_rules.select_related('user'):
                    if not ObjectAccess.objects.filter(user=rule.user,project_object__contracts=saved).exists():rule.delete()
                if not obj and not actor.is_leader:ContractAccess.objects.create(user=actor,contract=saved,granted_by=actor,**{f:True for f in SHARED_FIELDS})
                audit(actor,'contract_updated' if obj else 'contract_created',entity_id=str(saved.pk),summary=saved.number,linked=[str(oid) for oid in desired],unlinked=[str(oid) for oid in previous-desired])
                messages.success(request,'Договор сохранён.')
                return redirect('contract_detail',pk=saved.pk)
    return render(request,'projects/edit.html',{'form':form,'title':'Изменить контракт заказчика' if obj else 'Новый контракт с заказчиком','back':'contracts'})

@permission_required('projects.access')
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
    form=ContractShareForm(request.POST or None,contract=contract,initial=initial,actor=request.user)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            actor=User.objects.get(pk=request.user.pk)
            if not permitted(actor,'projects.access') or not visible_contracts(actor).filter(pk=contract.pk).exists():raise PermissionDenied
            if not actor.is_leader:
                own=contract.access_rules.filter(user=actor).first()
                if any(not own or not getattr(own,f) for f in form.cleaned_data['fields']):raise PermissionDenied
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
