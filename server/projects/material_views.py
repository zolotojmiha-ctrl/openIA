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
from repairs.excel import workbook
from . import repair_materials as materials, stock
from .models import CoatingProfile,StockItem,RepairConsumption,StockMovement,COMPANY_TAX_ID

def consumption_movements(job_ids):
    # Legacy manual reversals may carry the job only on their original movement.
    return StockMovement.objects.filter(item__legal_entity__tax_id=COMPANY_TAX_ID).filter(Q(kind='consume',job_id__in=job_ids)|Q(kind='reversal',reverses__kind='consume',reverses__job_id__in=job_ids)).select_related('item','job__section','job__contract','reverses__job__section','reverses__job__contract')

class ProfileForm(forms.ModelForm):
    expected_version=forms.IntegerField(widget=forms.HiddenInput,required=False)
    class Meta:
        model=CoatingProfile
        fields=['method','diameter_mm','length_mm','full_length_mm','area_per_m','extra_length_mm','tape','wrapper','primer','basis','active']
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs)
        for key in ['tape','wrapper','primer']:self.fields[key].queryset=stock.items(user).filter(archived=False,unit='kg')
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

@login_required
def listing(request):
    if not materials.can_read(request.user):raise PermissionDenied
    ids=set(visible_jobs(request.user).values_list('pk',flat=True))
    rows=[d for d in materials.all_demand() if d['job'].pk in ids]
    if permitted(request.user,'stock.manage'):
        from .p1_composition import choices
        for row in rows:row['assemblies']=choices(row['job'],request.user) if row['job'].method=='p1' else []
    ledger=RepairConsumption.objects.filter(job_id__in=ids).select_related('job__section','report').order_by('-updated_at')[:100]
    actual=list(consumption_movements(ids).order_by('-created_at')[:100])
    for row in actual:row.repair_job=row.job or row.reverses.job
    return render(request,'projects/repair_materials.html',{'actual':actual,'rows':rows,'ledger':ledger,'can_manage_materials':permitted(request.user,'stock.manage')})

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
    return render(request,'projects/coating_profiles.html',{'form':form,'profiles':CoatingProfile.objects.all(),'editing':profile})

@login_required
@require_POST
def retry(request,pk):
    job=get_object_or_404(visible_jobs(request.user),pk=pk)
    try:materials.retry(request.user,job)
    except ValidationError as e:return HttpResponse('; '.join(e.messages),status=400)
    return redirect('repair_materials')

@login_required
def export(request):
    if not materials.can_read(request.user):raise PermissionDenied
    allowed=set(visible_jobs(request.user).values_list('pk',flat=True));rows=[]
    for d in materials.all_demand():
        job=d['job']
        if job.pk not in allowed:continue
        label='; '.join('км '+x.km+' № '+x.number for x in job.active_defects)
        for n in d['couplings']+d['materials']:rows.append([job.contract.number if job.contract else '',job.section.project_object.name,job.section.pipeline,label,str(job.pk),'Потребность',n['name'],n['unit'],str(n['need']),str(n['missing']),''])
        for issue in d['issues']:rows.append(['',job.section.project_object.name,job.section.pipeline,label,str(job.pk),'Нужно уточнить','','','','',issue])
    for m in consumption_movements(allowed):
        job=m.job or m.reverses.job;label='; '.join('км '+x.km+' № '+x.number for x in job.active_defects)
        rows.append([job.contract.number if job.contract else '',job.section.project_object.name,job.section.pipeline,label,str(job.pk),'Списание' if m.kind=='consume' else 'Отмена',m.item.name,m.item.get_unit_display(),str(-m.quantity if m.kind=='reversal' else m.quantity),'',m.note])
    response=HttpResponse(workbook(rows,['Контракт','Объект','Нефтепровод','Дефекты общей конструкции','Код ремонта','Вид записи','Материал','Единица','Количество / норма (отмена со знаком минус)','К поставке','Основание'],'Материалы'),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');response['Content-Disposition']='attachment; filename="repair-materials.xlsx"';return response

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
