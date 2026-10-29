"""The defect register and warehouse use the same physical coupling reservations."""
import hashlib,json,uuid
from collections import defaultdict
from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404,render,redirect
from django.contrib import messages
from django.urls import reverse
from accounts.permissions import permission_required,permitted
from projects import stock,stock_groups,stock_planning,stock_fit
from projects.models import StockItem,StockActionReceipt,Contract
from .access import visible_jobs
from .models import CouplingUse
from .navigation import return_url

SALT='repair-stock-selection-v1'
COUPLING_METHODS=('p1','p2','p3','p4','p5u')


def summaries(user,jobs):
    """Expose only the material assigned to visible jobs, never general stock."""
    result={j.pk:{'rows':[],'reserved':0,'installed':0,'needed':len(stock_planning.requirements(j) or [])} for j in jobs}
    buckets=defaultdict(dict)
    def add(job_id,method,diameter,length,wall,warehouse,serial,state):
        key=(method,diameter,length,str(wall or ''),warehouse,state)
        row=buckets[job_id].setdefault(key,{'method':method,'diameter':diameter,'length':length,'wall':wall,'warehouse':warehouse,'state':state,'quantity':0,'serials':[]})
        row['quantity']+=1
        if serial:row['serials'].append(serial)
        result[job_id][state]+=1
    for item in StockItem.objects.filter(reserved_job_id__in=result,deleted_at__isnull=True).exclude(coupling__installation__isnull=False).prefetch_related('balances__warehouse'):
        balance=next((b for b in item.balances.all() if b.quantity>0),None)
        if balance:add(item.reserved_job_id,item.method_label,item.diameter_mm,item.length_mm,item.wall_mm,balance.warehouse.name,item.serial,'reserved')
    for use in CouplingUse.objects.filter(job_id__in=result).select_related('coupling','coupling__stock_item'):
        c=use.coupling;i=getattr(c,'stock_item',None)
        add(use.job_id,c.get_method_display(),i.diameter_mm if i else None,c.length_mm,c.wall_mm,c.warehouse,c.serial,'installed')
    can_manage=permitted(user,'stock.manage')
    for job in jobs:
        data=result[job.pk];data['rows']=list(buckets[job.pk].values());data['covered']=data['reserved']+data['installed']
        data['can_choose']=can_manage and job.method in COUPLING_METHODS and job.state not in ('reported','confirmed','unrepairable') and (not job.contract_id or job.contract.status!='archived') and job.section.project_object.status!='archived'
    return result


def candidates(user,job):
    result=[]
    lengths=stock_planning.requirements(job)
    for group in stock_groups.groups(user,include_archived=True):
        item=group['item'];wh=group['warehouse']
        if item.kind!='coupling' or not wh:continue
        members=[m for m in group['members'] if m['quantity']>0 and m['item'].reserved_job_id in (None,job.pk) and m['state']!='installed']
        own=sum(int(m['quantity']) for m in members if m['item'].reserved_job_id==job.pk)
        free=sum(int(m['quantity']) for m in members if m['item'].reserved_job_id is None and not m['item'].archived)
        if not own and (not free or wh.archived or item.archived):continue
        reason=stock_fit.reason(item,job)
        if not reason and (lengths is None or not any(stock_fit.length_fits(job.method,item.length_mm,n) for n in lengths)):reason='Недостаточная длина или другой состав конструкции.'
        if wh.archived or item.archived:reason='Склад или материал в архиве: можно только снять закрепление.'
        if wh.brigade_assignment_id and wh.project_object_id!=job.section.project_object_id:reason='Муфта у бригады другого участка: сначала оформите перемещение.'
        result.append({'group':group,'members':members,'own':own,'free':free,'reason':reason})
    return sorted(result,key=lambda r:(bool(r['reason']),r['group']['warehouse'].name,r['group']['item'].length_mm or 0,r['group']['id']))


class SelectionForm(forms.Form):
    snapshot=forms.CharField(widget=forms.HiddenInput)
    operation=forms.UUIDField(widget=forms.HiddenInput,initial=uuid.uuid4)
    def __init__(self,*args,user,job,rows,**kwargs):
        super().__init__(*args,**kwargs)
        self.initial['snapshot']=signing.dumps({'actor':str(user.pk),'job':str(job.pk),'version':job.version,'groups':{r['group']['id']:r['group']['token'] for r in rows}},salt=SALT,compress=True)
        for row in rows:
            key='quantity_'+row['group']['id']
            maximum=row['own'] if row['reason'] else row['own']+row['free']
            self.fields[key]=forms.IntegerField(label='Закрепить, шт.',min_value=0,max_value=maximum,initial=row['own'],required=False,widget=forms.NumberInput(attrs={'min':0,'max':maximum,'inputmode':'numeric','data-stock-quantity':'1'}))
            row['field']=self[key]


@transaction.atomic
def save(user,job_id,snapshot,counts,operation):
    actor=stock.manager(user);stock.lock()
    scope=get_object_or_404(visible_jobs(actor),pk=job_id)
    if scope.contract_id:Contract.objects.select_for_update(no_key=True).get(pk=scope.contract_id)
    job=get_object_or_404(visible_jobs(actor).select_for_update(no_key=True,of=('self',)),pk=job_id)
    try:
        payload=signing.loads(snapshot,salt=SALT)
        if payload['actor']!=str(actor.pk) or payload['job']!=str(job.pk):raise ValueError
        tokens=payload['groups']
        if set(counts)!=set(tokens):raise ValueError
    except (signing.BadSignature,KeyError,TypeError,ValueError):raise ValidationError('Выбор изменён или повреждён. Откройте выбор муфт заново.')
    digest=hashlib.sha256(json.dumps([snapshot,counts],sort_keys=True).encode()).hexdigest()
    previous=StockActionReceipt.objects.filter(token=operation).first()
    if previous:
        if previous.actor_id!=actor.pk or previous.digest!=digest:raise ValidationError('Эта отправка уже использована с другими данными.')
        return
    if job.version!=payload['version']:raise ValidationError('Параметры дефекта изменились. Откройте выбор муфт заново.')
    if job.method not in COUPLING_METHODS or job.state in ('reported','confirmed','unrepairable') or job.contract_id and job.contract.status=='archived' or job.section.project_object.status=='archived':raise ValidationError('Работа завершена или закрыта. Изменение установленной конструкции выполняется через исправление результата.')
    current=stock_groups.groups(actor,include_archived=True);changes=[]
    for key,token in tokens.items():
        group=stock_groups.current(actor,token,current)
        if group['id']!=key:raise ValidationError('Выбор материала повреждён.')
        members=[m for m in group['members'] if m['quantity']>0 and m['item'].reserved_job_id in (None,job.pk) and m['state']!='installed']
        own=sum(int(m['quantity']) for m in members if m['item'].reserved_job_id==job.pk)
        wanted=counts[key]
        if not isinstance(wanted,int) or wanted<0:raise ValidationError('Укажите целое неотрицательное количество.')
        if wanted>sum(int(m['quantity']) for m in members):raise ValidationError('Свободный остаток изменился.')
        if wanted!=own:changes.append((key,wanted,own,[m['item'].pk for m in members]))
    # Release surplus before reserving replacements, inside the same transaction.
    for key,wanted,own,ids in changes:
        if wanted<own:
            group=next(g for g in current if g['id']==key)
            stock_groups.distribute(actor,group['token'],{str(job.pk):wanted},ids)
    current={g['id']:g for g in stock_groups.groups(actor,include_archived=True)}
    for key,wanted,own,ids in changes:
        if wanted>own:
            group=current[key]
            if group['item'].archived or group['warehouse'].archived:raise ValidationError('Нельзя закрепить материал из архива.')
            stock_groups.distribute(actor,group['token'],{str(job.pk):wanted},ids)
    StockActionReceipt.objects.create(token=operation,actor=actor,digest=digest)


@permission_required('stock.manage')
def choose(request,pk):
    job=get_object_or_404(visible_jobs(request.user),pk=pk)
    back=return_url(request,reverse('work_place',args=[job.contract_id,job.section.project_object_id]) if job.contract_id else reverse('repairs'))
    rows=candidates(request.user,job);form=SelectionForm(request.POST or None,user=request.user,job=job,rows=rows);status=200
    if request.method=='POST' and form.is_valid():
        counts={r['group']['id']:form.cleaned_data['quantity_'+r['group']['id']] or 0 for r in rows}
        try:save(request.user,pk,form.cleaned_data['snapshot'],counts,form.cleaned_data['operation'])
        except ValidationError as exc:form.add_error(None,exc);status=409
        else:messages.success(request,'Муфты закреплены. Они остаются на складе до монтажа.');return redirect(back)
    if request.method=='POST' and form.errors and status==200:status=400
    return render(request,'repairs/stock_choose.html',{'job':job,'rows':rows,'form':form,'back':back,'stock_summary':summaries(request.user,[job])[job.pk]},status=status)
