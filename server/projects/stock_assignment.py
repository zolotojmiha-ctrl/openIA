"""Visible defect table for assigning a group or a particular factory number."""
import json,uuid
from collections import Counter
from decimal import Decimal
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError,ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from django.shortcuts import render,redirect
from repairs.access import visible_jobs
from . import stock,stock_groups as groups,stock_planning as planning

def selection(group,item_id):
    if not item_id:return group
    try:pk=uuid.UUID(str(item_id))
    except (ValueError,TypeError):raise ValidationError('Неизвестная муфта.')
    chosen=groups.selected(group,{pk})
    if len(chosen['members'])!=1 or chosen['quantity']!=1:raise ValidationError('Для индивидуального назначения нужна одна муфта на складе.')
    return chosen

@transaction.atomic
def save(user,token,item_id,targets,serial=None):
    actor=stock.manager(user);stock.lock();full=groups.current(actor,token);chosen=selection(full,item_id)
    if not isinstance(targets,dict) or len(targets)>2000:raise ValidationError('Неверный список назначений.')
    cleaned={}
    for key,value in targets.items():
        try:pk=uuid.UUID(key);q=forms.IntegerField(min_value=0,max_value=1000000).clean(value)
        except (ValueError,TypeError,ValidationError):raise ValidationError('Укажите целые неотрицательные количества.')
        if q:cleaned[str(pk)]=q
    if item_id and serial is not None:
        if not isinstance(serial,str) or len(serial)>100:raise ValidationError('Заводской номер: до 100 символов.')
        i=stock.items(actor).get(pk=chosen['members'][0]['item'].pk)
        if i.serial!=serial.strip():
            data=groups.values(i);data.update(serial=serial.strip(),coupling=i.coupling);stock.save_item(actor,i.pk,i.version,data)
            full=next(g for g in groups.groups(actor) if g['id']==full['id'])
    groups.distribute(actor,full['token'],cleaned,item_ids={m['item'].pk for m in chosen['members']} if item_id else None)

def job_rows(user,group):
    held={str(d['job'].pk):int(d['quantity']) for d in group['distribution'] if d['state']=='reserved'}
    active=~Q(state__in=['reported','confirmed'])&~Q(contract__status='archived')&~Q(section__project_object__status='archived')
    jobs=visible_jobs(user,include_archived=True).filter(active|Q(pk__in=held)).select_related('section__project_object','contract','assigned_to').prefetch_related('defects','reserved_stock','installed_couplings__coupling')
    own={m['item'].pk for m in group['members']};rows=[];i=group['item']
    for job in jobs:
        parts=planning.requirements(job);current=held.get(str(job.pk),0);reason=''
        if job.state in ('reported','confirmed') or job.contract and job.contract.status=='archived' or job.section.project_object.status=='archived':reason='Работа завершена или закрыта.'
        elif job.method!=i.method:reason='Другой тип конструкции.'
        elif job.wall_mm!=Decimal(i.wall_mm or 0):reason='Другая толщина стенки.'
        elif job.section.diameter_mm and job.section.diameter_mm!=i.diameter_mm:reason='Другой диаметр.'
        elif parts is None:reason='Нужно уточнить длину и состав конструкции.'
        elif i.length_mm not in parts:reason='Для конструкции нужна другая длина муфты.'
        others=[s for s in job.reserved_stock.all() if s.pk not in own and s.deleted_at is None]
        counted={s.coupling_id for s in others};used=sum(s.length_mm==i.length_mm for s in others)
        used+=sum(u.coupling.length_mm==i.length_mm for u in job.installed_couplings.all() if u.coupling_id not in counted)
        need=max(0,Counter(parts or [])[i.length_mm]-used)
        if not reason and not need and not current:reason='Потребность уже закрыта.'
        defects=job.active_defects
        rows.append({'job':job,'defects':defects,'current':current,'maximum':max(current,need) if not reason else current,'reason':reason,'compatible':not reason,'search':' '.join([planning.job_label(job),job.get_method_display(),str(job.length_m or ''),job.composition,*(d.pipe_section for d in defects)]).casefold()})
    return sorted(rows,key=lambda r:(r['job'].contract.number if r['job'].contract else '',r['job'].section.pipeline,min((d.kilometre for d in r['defects']),default=Decimal(0)),str(r['job'].pk)))

def view(request):
    token=request.POST.get('group','') if request.method=='POST' else request.GET.get('group','')
    item_id=request.POST.get('item','') if request.method=='POST' else request.GET.get('item','')
    try:
        full=groups.current(request.user,token)
        active=[m for m in full['members'] if m['quantity']>0 and m['state']!='installed']
        if not item_id and len(active)==1 and active[0]['quantity']==1:item_id=str(active[0]['item'].pk)
        chosen=selection(full,item_id)
        if chosen['item'].kind!='coupling' or not chosen['warehouse'] or chosen['quantity']<=0:raise ValidationError('Сначала укажите склад и количество в режиме «Редактировать список».')
    except ValidationError as e:messages.error(request,'; '.join(e.messages));return redirect('stock_inventory')
    status=200;error=''
    if request.method=='POST':
        try:
            targets=json.loads(request.POST.get('targets','{}'));save(request.user,token,item_id,targets,request.POST.get('serial') if item_id else None)
        except (ValidationError,ValueError,TypeError,ObjectDoesNotExist) as e:error='; '.join(e.messages) if isinstance(e,ValidationError) else 'Проверьте выбранные назначения.';status=409
        else:messages.success(request,'Муфты закреплены за выбранными дефектами. Они остаются на складе до монтажа.');return redirect('stock_inventory')
    rows=job_rows(request.user,chosen)
    if error:
        try:
            posted=json.loads(request.POST.get('targets','{}'))
            if isinstance(posted,dict):
                for row in rows:row['current']=forms.IntegerField(min_value=0).clean(posted.get(str(row['job'].pk),0))
        except (ValueError,TypeError,ValidationError):pass
    return render(request,'projects/stock_assignment.html',{'group':chosen,'token':token,'item_id':item_id,'rows':rows,'error':error,'serial':request.POST.get('serial',chosen['members'][0]['serial']) if item_id else ''},status=status)
