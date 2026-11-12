from projects.diameters import DiameterField,preserve_legacy
from decimal import Decimal
from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404,render,redirect
from django.views.decorators.http import require_POST
from accounts.permissions import permitted
from accounts.views import audit
from repairs.access import visible_jobs
from repairs.models import Job
from .report_excel import workbook
from . import repair_materials as materials, stock
from .models import CoatingProfile,StockItem,RepairConsumption,StockMovement,COMPANY_TAX_ID

def consumption_movements(job_ids):
    # Legacy manual reversals may carry the job only on their original movement.
    return StockMovement.objects.filter(item__legal_entity__tax_id=COMPANY_TAX_ID).filter(Q(kind='consume',job_id__in=job_ids)|Q(kind='reversal',reverses__kind='consume',reverses__job_id__in=job_ids)).select_related('item','job__section','job__contract','reverses__job__section','reverses__job__contract')

class ProfileForm(forms.ModelForm):
    diameter_mm=DiameterField()
    expected_version=forms.IntegerField(widget=forms.HiddenInput,required=False)
    class Meta:
        model=CoatingProfile
        fields=['method','diameter_mm','length_mm','full_length_mm','area_per_m','extra_length_mm','tape','wrapper','primer','basis','active']
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs)
        preserve_legacy(self.fields['diameter_mm'],self.instance.diameter_mm)
        for key in ['tape','wrapper','primer']:self.fields[key].queryset=stock.items(user).filter(archived=False,unit='kg')
        if not self.instance.pk or self.instance.method=='p2':self.fields['method'].choices=[('p2','П2')]
        self.fields['area_per_m'].help_text='Подтверждённая площадь на метр для этого диаметра/конструкции. Пример 2,55 не переносится на все трубы.'
        self.fields['full_length_mm'].help_text='Полный габарит с кольцами, а не требуемая длина дефекта. Для вашего примера П2 1500 мм полный габарит 1960 мм.'
    def clean(self):
        c=super().clean()
        if c.get('full_length_mm',0)<c.get('length_mm',0):self.add_error('full_length_mm','Полный габарит не меньше номинальной длины.')
        if c.get('area_per_m') is not None and c['area_per_m']<=0:self.add_error('area_per_m','Площадь должна быть положительной.')
        for key in ('tape','wrapper','primer'):
            item=c.get(key)
            if item and item.unit!='kg':self.add_error(key,'Нужна позиция в кг.')
        if len({c[k].pk for k in ('tape','wrapper','primer') if c.get(k)})!=3:raise forms.ValidationError('Лента, обёртка и праймер — три отдельные позиции.')
        return c

def demand_report(user):
    ids=set(visible_jobs(user).values_list('pk',flat=True))
    rows=[d for d in materials.all_demand() if d['job'].pk in ids]
    from .demand_details import location,order
    grouped={};issues=[]
    for row in rows:
        job=row['job'];label='; '.join('км '+d.km+' № '+d.number for d in job.active_defects)
        if row['issues']:issues.append({'job':job,'label':label,'reason':friendly_issue(job,row['issues']),'missing_profile':any('площадь покрытия' in s for s in row['issues']),'missing_p1_norm':any('В таблице ПТО нет' in s for s in row['issues'])})
        for n in row['couplings']+row['materials']:
            if not n['missing']:continue
            key=(str(job.section.project_object_id),str(job.section_id),n['name'],n['unit']);r=grouped.setdefault(key,{'scope_id':(str(job.section.project_object_id),str(job.section_id)),'object':job.section.project_object.name,'section':job.section.name,'name':n['name'],'unit':n['unit'],'need':Decimal(0),'missing':Decimal(0),'jobs':[],'placements':[]})
            r['need']+=Decimal(n['need']);r['missing']+=Decimal(n['missing'])
            r['placements'].append({**location(job),'job':job,'quantity':n['missing']})
            if n['missing']:r['jobs'].append({'job':job,'label':label,'quantity':n['missing']})
    for group in grouped.values():group['placements'].sort(key=order)
    return rows,sorted(grouped.values(),key=lambda n:(n['object'],n['section'],n['scope_id'],n['name'])),issues

@login_required
def listing(request):
    if not materials.can_read(request.user):raise PermissionDenied
    rows,grouped,issues=demand_report(request.user)
    if permitted(request.user,'stock.manage'):
        from .p1_composition import choices
        for row in rows:row['assemblies']=choices(row['job'],request.user) if row['job'].method=='p1' else []
    return render(request,'projects/repair_materials.html',{'grouped':grouped,'issues':issues,'rows':rows,'can_manage_materials':permitted(request.user,'stock.manage')})

def friendly_issue(job,issues):
    if not job.section.diameter_mm:return 'Нужно указать диаметр трубопровода.'
    if not job.wall_mm:return 'Нужно указать толщину стенки конструкции.'
    if any('В таблице ПТО нет' in s for s in issues):return 'В таблице ПТО нет нормы покрытия П1 для этой длины или диаметра.'
    if any('площадь покрытия' in s for s in issues):return 'Для этого типоразмера ещё не заданы нормы изоляции.'
    return 'Нужно уточнить размеры конструкции или норму расхода.'

def job_panel(user,job):
    if not materials.can_read(user):return None
    total={}
    for m in consumption_movements([job.pk]):
        key=m.item_id;r=total.setdefault(key,{'name':m.item.name,'unit':m.item.get_unit_display(),'quantity':Decimal(0)})
        r['quantity']+=-m.quantity if m.kind=='reversal' else m.quantity
    geometry=None
    if job.method=='p1':
        from .p1_coating import row,dimensions
        pieces=[u.coupling for u in job.installed_couplings.select_related('coupling')] if job.state in ('reported','confirmed') else None
        try:geometry=row(job.section.diameter_mm,dimensions(job,pieces))
        except ValidationError:pass
    entry=RepairConsumption.objects.filter(job=job).first()
    actual_coating=next((line for line in entry.lines if line.get('actual_cleaning_length_m') or line.get('actual_coating_length_m')),None) if entry else None
    return {'actual_coating':actual_coating,'totals':[r for r in total.values() if r['quantity']],'entry':entry,'can_manage':permitted(user,'stock.manage'),'coating_geometry':geometry}

@login_required
def profiles(request,pk=None):
    if not permitted(request.user,'stock.manage'):raise PermissionDenied
    profile=get_object_or_404(CoatingProfile,pk=pk) if pk else None
    form=ProfileForm(request.POST or None,instance=profile,user=request.user,initial={'expected_version':profile.version if profile else 0})
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            actor=stock.manager(request.user);stock.lock()
            old=CoatingProfile.objects.select_for_update().filter(pk=pk).first() if pk else None
            if old and old.version!=form.cleaned_data['expected_version']:form.add_error(None,'Профиль изменён. Обновите страницу.')
            else:
                row=form.save(commit=False);row.created_by=old.created_by if old else actor;row.version=(old.version if old else 0)+1;row.save();audit(actor,'stock_updated',kind='coating_profile',profile=str(row.pk));return redirect('coating_profiles')
    from .p1_coating import TABLE,row
    return render(request,'projects/coating_profiles.html',{'form':form,'profiles':CoatingProfile.objects.all(),'editing':profile,'p1_rows':[row(720,int(length)) for length in TABLE['rows']]})

@login_required
@require_POST
def retry(request,pk):
    job=get_object_or_404(visible_jobs(request.user),pk=pk)
    try:materials.retry(request.user,job)
    except ValidationError as e:return HttpResponse('; '.join(e.messages),status=400)
    return redirect('repair_detail',pk=job.pk)

@login_required
def export(request):
    if not materials.can_read(request.user):raise PermissionDenied
    records,grouped,issues=demand_report(request.user)
    allowed=set(visible_jobs(request.user).values_list('pk',flat=True));details=[];actual=[]
    headers=['Контракт','Объект','Нефтепровод','Дефекты общей конструкции','Код ремонта','Вид записи','Материал','Единица','Количество / норма (отмена со знаком минус)','К поставке','Основание']
    for record in records:
        job=record['job'];label='; '.join('км '+x.km+' № '+x.number for x in job.active_defects)
        for n in record['couplings']+record['materials']:
            if not n['missing']:continue
            details.append([job.contract.number if job.contract else '',job.section.project_object.name,job.section.pipeline,label,str(job.pk),'Потребность',n['name'],n['unit'],Decimal(n['missing']),Decimal(n['missing']),''])
    for m in consumption_movements(allowed):
        job=m.job or m.reverses.job;label='; '.join('км '+x.km+' № '+x.number for x in job.active_defects)
        actual.append([job.contract.number if job.contract else '',job.section.project_object.name,job.section.pipeline,label,str(job.pk),'Списание' if m.kind=='consume' else 'Отмена',m.item.name,m.item.get_unit_display(),-m.quantity if m.kind=='reversal' else m.quantity,'',m.note])
    sheets=[
        ('Потребность',['Материал / конструкция','Единица','Нужно','Объект','Участок'],[[n['name'],n['unit'],n['missing'],n['object'],n['section']] for n in grouped]),
        ('По работам',headers,details),
        ('Нужно уточнить',['Объект','Работы','Причина'],[[r['job'].section.project_object.name,r['label'],r['reason']] for r in issues]),
        ('Расход',headers,actual),
    ]
    response=HttpResponse(workbook(sheets),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');response['Content-Disposition']='attachment; filename="repair-materials.xlsx"';response['Cache-Control']='private, no-store';return response

@login_required
@require_POST
def choose_composition(request,pk):
    from .p1_composition import apply
    job=get_object_or_404(visible_jobs(request.user),pk=pk)
    try:apply(request.user,job.pk,request.POST.get('choice',''))
    except ValidationError as e:
        from django.contrib import messages
        messages.error(request,'; '.join(e.messages))
    return redirect('repair_materials')
