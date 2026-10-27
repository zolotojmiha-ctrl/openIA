from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import render,get_object_or_404,redirect
from django.urls import reverse
from accounts.models import User
from accounts.views import audit
from .models import Defect,Section,Job
from .access import prepare_required,visible_jobs
from .values import kilometre,identifier
FIELDS=['kilometre','number','pipe_section','description','latitude','longitude']

def snapshot(d):return {k:str(getattr(d,k) if getattr(d,k) is not None else '') for k in FIELDS}

def visible_defects(user):return Defect.objects.filter(jobs__in=visible_jobs(user,include_archived=True)).distinct()

class DefectForm(forms.ModelForm):
    expected_snapshot=forms.CharField(widget=forms.HiddenInput)
    class Meta:
        model=Defect;fields=FIELDS;widgets={'description':forms.Textarea(attrs={'rows':3})}
    def clean_kilometre(self):return kilometre(self.cleaned_data['kilometre'])
    def clean_number(self):return identifier(self.cleaned_data['number'],'Номер дефекта')
    def clean_pipe_section(self):return identifier(self.cleaned_data.get('pipe_section',''),'Номер секции',optional=True)

@prepare_required
def edit(request,pk):
    defect=get_object_or_404(visible_defects(request.user),pk=pk)
    back=reverse('work_place',args=[defect.contract_id,defect.section.project_object_id]) if defect.contract_id else reverse('repairs')
    token=signing.dumps({'actor':str(request.user.pk),'defect':str(pk),'values':snapshot(defect)},salt='defect-edit')
    form=DefectForm(request.POST or None,instance=defect,initial={'expected_snapshot':token})
    if request.method=='POST' and form.is_valid():
        try:
            expected=signing.loads(form.cleaned_data['expected_snapshot'],salt='defect-edit',max_age=8*3600)
            with transaction.atomic():
                User.objects.select_for_update().get(pk=request.user.pk)
                from projects.archive import writable
                writable([defect.contract_id]);Section.objects.select_for_update().get(pk=defect.section_id)
                jobs=list(Job.objects.select_for_update().filter(defects=pk).order_by('pk'))
                locked=get_object_or_404(Defect.objects.select_for_update().filter(pk__in=visible_defects(request.user).values('pk')),pk=pk)
                before=snapshot(locked)
                if expected!={'actor':str(request.user.pk),'defect':str(pk),'values':before}:raise ValidationError('Дефект уже изменён. Обновите страницу.')
                identity_changed=(form.cleaned_data['kilometre']!=locked.kilometre or any(form.cleaned_data[k]!=getattr(locked,k) for k in ['number','pipe_section']))
                if identity_changed and any(j.reports.exists() or j.state in ['reported','confirmed'] for j in jobs):raise ValidationError('По дефекту уже есть результаты. Километр и номер сохраняются в истории; для другой точки создайте новый дефект.')
                for k in FIELDS:setattr(locked,k,form.cleaned_data[k])
                locked.full_clean();locked.save()
                for job in jobs:job.version+=1;job.save(update_fields=['version'])
                audit(request.user,'repair_prepared',kind='defect_edit',defect=str(pk),previous=before,values=snapshot(locked))
        except (signing.BadSignature,ValidationError) as e:form.add_error(None,e if isinstance(e,ValidationError) else 'Форма устарела. Обновите страницу.')
        else:return redirect(back)
    return render(request,'projects/operation_form.html',{'form':form,'title':'Изменить данные дефекта','back':back,'intro':'Номер, километр и секция меняются до появления результатов. Описание и координаты можно уточнять с записью в истории.'})
