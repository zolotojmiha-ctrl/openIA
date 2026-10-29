from accounts import xlsx as human_xlsx
import uuid
from decimal import Decimal
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render,redirect,get_object_or_404
from django.utils import timezone
from django.urls import reverse
from accounts.models import User
from .models import Contract,DailyWork,WORK_STAGES,WORK_KINDS,DailyWorkImportReceipt
from .access import visible_contracts,visible_objects,contract_card
from . import daily
from repairs.excel import workbook
from repairs.importer import read_book

HEADERS=['ID записи','Версия','Дата','Этап','Секция / место','Км от','Км до','Длина, м','Площадь, м²','Ширина, мм','Диаметр, мм','Количество','Ед.','Простой, ч','Причина','Примечание','ID назначения бригады','ID указания заказчика','Купершлак, кг (справочно)']
def scope(request,pk,object_id,kind):
    contract=get_object_or_404(visible_contracts(request.user),pk=pk)
    obj=get_object_or_404(visible_objects(request.user).filter(contracts=contract),pk=object_id)
    if kind not in WORK_STAGES or kind not in contract.work_kinds:raise PermissionDenied
    return contract,obj

def link(contract,obj,kind):return reverse('daily_works',args=[contract.pk,obj.pk,kind])

class DecisionForm(forms.Form):
    version=forms.IntegerField(widget=forms.HiddenInput)
    action=forms.ChoiceField(label='Решение',choices=[('confirmed','Подтвердить'),('revision','Вернуть на доработку')])
    note=forms.CharField(label='Пояснение',max_length=2000,required=False,widget=forms.Textarea(attrs={'rows':3}))

@login_required
def listing(request,pk,object_id,kind):
    contract,obj=scope(request,pk,object_id,kind)
    records=daily.works(request.user).filter(contract=contract,project_object=obj,kind=kind).order_by('-current_revision__created_at')
    reported={str(r.pk):r.version for r in records.filter(state='reported')}
    snapshot=signing.dumps({'actor':str(request.user.pk),'versions':reported,'contract':str(pk),'object':str(object_id),'kind':kind},salt='daily-bulk')
    if request.method=='POST':
        if not request.user.is_leader:raise PermissionDenied
        try:
            shown=signing.loads(request.POST.get('snapshot',''),salt='daily-bulk',max_age=3600)
            if shown['actor']!=str(request.user.pk) or shown['contract']!=str(pk) or shown['object']!=str(object_id) or shown['kind']!=kind:raise PermissionDenied
            ids=list(shown['versions']) if request.POST.get('all')=='1' else request.POST.getlist('selected')
            if not ids or any(i not in shown['versions'] for i in ids):raise ValidationError('Выберите записи на проверке.')
            with transaction.atomic():
                for key in sorted(set(ids)):daily.decide(request.user,key,shown['versions'][key],'confirmed')
        except (signing.BadSignature,KeyError,ValidationError) as e:messages.error(request,'; '.join(e.messages) if isinstance(e,ValidationError) else 'Выбор устарел. Обновите страницу.')
        else:messages.success(request,'Выбранные выполнения подтверждены.');return redirect(link(contract,obj,kind))
    totals={}
    for row in records.exclude(active_revision=None):
        data=row.active_revision.data;stage=data.get('stage');total=totals.setdefault((stage,data.get('unit','')),{'label':dict(WORK_STAGES[kind]).get(stage,stage),'length':Decimal(0),'area':Decimal(0),'downtime':Decimal(0),'abrasive':Decimal(0),'quantity':Decimal(0),'unit':data.get('unit','')})
        for key in ('length','area','downtime','abrasive','quantity'):total[key]+=Decimal(data.get(key) or '0')
    return render(request,'projects/daily_list.html',{'card':contract_card(contract,request.user),'object':obj,'kind':kind,'kind_name':dict(WORK_KINDS)[kind],'records':records[:200],'snapshot':snapshot,'totals':list(totals.values()),'total_count':records.count(),'readonly':contract.status=='archived'})

@login_required
def edit(request,pk,object_id,kind,record_id=None):
    contract,obj=scope(request,pk,object_id,kind)
    row=get_object_or_404(daily.works(request.user).filter(contract=contract,project_object=obj,kind=kind),pk=record_id) if record_id else None
    if row and not daily.prepare(request.user) and row.author_id!=request.user.pk:raise PermissionDenied
    initial=dict(row.current_revision.data) if row else {'date':timezone.localdate()}
    initial.update(record=row.pk if row else uuid.uuid4(),version=row.version if row else 0)
    form=daily.WorkForm(request.POST or None,user=request.user,contract=contract,obj=obj,kind=kind,initial=initial)
    if request.method=='POST' and form.is_valid():
        try:result=daily.submit(request.user,pk,object_id,kind,request.POST)
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('daily_detail',record_id=result.pk)
    return render(request,'projects/daily_form.html',{'form':form,'card':contract_card(contract,request.user),'object':obj,'kind':kind,'row':row})

@login_required
def detail(request,record_id):
    row=get_object_or_404(daily.works(request.user),pk=record_id)
    form=DecisionForm(request.POST or None,initial={'version':row.version})
    if request.method=='POST':
        if not request.user.is_leader:raise PermissionDenied
        if form.is_valid():
            c=form.cleaned_data
            try:daily.decide(request.user,row.pk,c['version'],c['action'],c['note'])
            except ValidationError as e:form.add_error(None,e)
            else:return redirect('daily_detail',record_id=row.pk)
    history=[]
    from .crews import visible_assignments
    crew_names={str(a.pk):str(a) for a in visible_assignments(request.user).filter(contract=row.contract,project_object=row.project_object)}
    for r in row.revisions.select_related('author','decision__actor').order_by('-number'):
        values=[]
        for field in daily.FIELDS:
            value=r.data.get(field,'')
            if field=='stage':value=dict(WORK_STAGES[row.kind]).get(value,value)
            if field=='crew':value=crew_names.get(value,'Назначение больше недоступно' if value else '')
            if field=='change' and value:
                from .models import ContractChangeRevision
                change=ContractChangeRevision.objects.filter(pk=value,change__contract=row.contract).first()
                value=f'{change.title} · редакция {change.number}' if change else 'Редакция недоступна'
            values.append((daily.WorkForm.base_fields[field].label,value))
        history.append({'revision':r,'values':values,'decision':getattr(r,'decision',None)})
    return render(request,'projects/daily_detail.html',{'row':row,'history':history,'form':form,'can_edit':(daily.prepare(request.user) or row.author_id==request.user.pk) and (row.state!='confirmed' or request.user.is_leader) and row.contract.status!='archived'})

class UploadForm(forms.Form):file=forms.FileField(label='Ведомость XLSX')

def apply_rows(user,contract,obj,kind,rows):
    for number,values in rows:
        data=dict(zip(daily.FIELDS,values[2:]));data.update(record=values[0] or str(uuid.uuid4()),version=values[1] or '0')
        # IDs for new rows are frozen by parse, so retrying preview cannot duplicate them.
        try:daily.submit(user,contract.pk,obj.pk,kind,data)
        except ValidationError as e:raise ValidationError(f'Строка {number}: '+ '; '.join(e.messages)) from e

def parse(file,user=None):
    book=read_book(file)
    if user:book=human_xlsx.normalize(user,'daily',book,HEADERS)
    if book['formulas']:raise ValidationError('Замените формулы значениями.')
    cells=book['cells']
    if [str(cells.get((1,i+1),'')).strip() for i in range(len(HEADERS))]!=HEADERS:raise ValidationError('Используйте шаблон выбранной ведомости.')
    rows=[];seen=set()
    for number in sorted({r for r,c in cells if r>1}):
        values=[str(cells.get((number,i+1),'')).strip() for i in range(len(HEADERS))]
        if not any(values):continue
        if len(rows)>=500:raise ValidationError('Не более 500 строк за импорт.')
        values[0]=values[0] or str(uuid.uuid4())
        if values[0] in seen:raise ValidationError('Повтор ID записи.')
        seen.add(values[0]);rows.append([number,values])
    if not rows:raise ValidationError('Нет строк для переноса.')
    return rows

@login_required
def excel(request,pk,object_id,kind):
    contract,obj=scope(request,pk,object_id,kind)
    if request.GET.get('export')=='1' or request.GET.get('template')=='1':
        rows=[] if request.GET.get('template')=='1' else [[str(r.pk),r.version,*[r.current_revision.data.get(k,'') for k in daily.FIELDS]] for r in daily.works(request.user).filter(contract=contract,project_object=obj,kind=kind)]
        response=HttpResponse(human_xlsx.workbook(request.user,'daily',rows,HEADERS),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');response['Content-Disposition']='attachment; filename="armada-daily-works.xlsx"';return response
    form=UploadForm(request.POST or None,request.FILES or None);preview='';rows=[]
    if request.method=='POST':
        try:
            if request.POST.get('apply')=='1':
                payload=signing.loads(request.POST.get('preview',''),salt='daily-excel',max_age=3600)
                if payload['actor']!=str(request.user.pk) or payload['session']!=request.user.session_version or payload['scope']!=[str(pk),str(object_id),kind]:raise PermissionDenied
                with transaction.atomic():
                    actor=User.objects.select_for_update().get(pk=request.user.pk)
                    if not actor.is_active or actor.session_version!=request.user.session_version:raise PermissionDenied
                    if not DailyWorkImportReceipt.objects.filter(token=payload['token'],actor=actor).exists():
                        apply_rows(actor,contract,obj,kind,payload['rows']);DailyWorkImportReceipt.objects.create(token=payload['token'],actor=actor)
                messages.success(request,'Ведомость перенесена на проверку.');return redirect(link(contract,obj,kind))
            if form.is_valid():
                rows=parse(form.cleaned_data['file'],request.user)
                with transaction.atomic():apply_rows(request.user,contract,obj,kind,rows);transaction.set_rollback(True)
                preview=signing.dumps({'actor':str(request.user.pk),'session':request.user.session_version,'scope':[str(pk),str(object_id),kind],'rows':rows,'token':str(uuid.uuid4())},salt='daily-excel',compress=True)
        except (signing.BadSignature,ValidationError,ValueError,KeyError) as e:form.add_error(None,e if isinstance(e,ValidationError) else 'Проверьте файл или повторите предварительный просмотр.')
    return render(request,'projects/daily_excel.html',{'form':form,'preview':preview,'rows':rows,'card':contract_card(contract,request.user),'object':obj,'kind':kind})

@login_required
def review_queue(request):
    if not request.user.is_leader:raise PermissionDenied
    return render(request,'projects/daily_review.html',{'records':daily.works(request.user).filter(state='reported').order_by('current_revision__created_at')})
