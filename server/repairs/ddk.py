"""Explicit method revision after DDK. Not a completion or stock write-off."""
import hashlib,json,uuid
from django import forms
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.shortcuts import render,redirect,get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from documents.storage import validate_upload,store_file,private_path
from accounts.models import User
from .access import reportable_jobs,can_prepare,visible_jobs
from .plan_views import ConstructionForm
from .forms import MultipleFiles,MultipleInput
from .models import Report,Attachment
from . import plans
from .construction import COUPLING_METHODS


class DDKForm(ConstructionForm):
    token=forms.UUIDField(widget=forms.HiddenInput)
    date=forms.DateField(label='Дата ДДК',widget=forms.DateInput(attrs={'type':'date'},format='%Y-%m-%d'))
    files=MultipleFiles(label='Заключение ДДК / фотографии',required=False,widget=MultipleInput())
    def clean(self):
        data=super().clean()
        if data.get('date') and data['date']>timezone.localdate():self.add_error('date','Дата ДДК не может быть в будущем.')
        if data.get('method') in COUPLING_METHODS:
            if not data.get('wall_mm'):self.add_error('wall_mm','Укажите толщину стенки новой конструкции.')
        else:data.update(length_m=None,wall_mm=None,composition='')
        return data


def allowed(user):
    return visible_jobs(user) if can_prepare(user) else reportable_jobs(user) if user.is_active and user.role=='foreman' else visible_jobs(user).none()


def change(user,pk,data):
    stored=[]
    try:
        with transaction.atomic():
            actor=User.objects.select_for_update(no_key=True).get(pk=user.pk)
            if not actor.is_active or actor.session_version!=user.session_version or not allowed(actor).filter(pk=pk).exists():raise PermissionDenied
            raw_files=data.get('files',[])
            check=DDKForm({k:data.get(k) for k in (*plans.FIELDS,'date','reason','token','expected_version')})
            if not check.is_valid():raise ValidationError(str(check.errors.as_text()))
            data=check.cleaned_data
            facts={k:str(data.get(k) or '') for k in (*plans.FIELDS,'date','reason')}
            from django.core.files.uploadedfile import SimpleUploadedFile
            uploads=[validate_upload(SimpleUploadedFile(f['name'],f['content'])) if isinstance(f,dict) else validate_upload(f) for f in raw_files]
            if sum(f['size'] for f in uploads)>40*1024*1024:raise ValidationError('Не более 40 МБ файлов за один раз.')
            if len(uploads)>20:raise ValidationError('Не более 20 файлов за один раз.')
            digest=hashlib.sha256(json.dumps({'facts':facts,'files':[(f['name'],f['sha256']) for f in uploads]},sort_keys=True).encode()).hexdigest()
            previous=Report.objects.filter(token=data['token']).first()
            if previous:
                if previous.job_id==pk and previous.author_id==actor.pk and previous.request_digest==digest:return previous
                raise ValidationError('Этот запрос уже сохранён с другими данными. Откройте актуальную форму.')
            values={k:data[k] for k in plans.FIELDS}
            candidate=allowed(actor).get(pk=pk)
            if candidate.state not in ('planned','progress','partial'):raise ValidationError('Работа уже передана на проверку или закрыта. Изменение выполняется через исправление результата.')
            if plans.snapshot(candidate)=={k:(format(v.normalize(),'f') if hasattr(v,'normalize') else str(v or '')) for k,v in values.items()}:raise ValidationError('Новые параметры совпадают с заданием.')
            job,_=plans.revise(actor,pk,data['expected_version'],values,'По результатам ДДК: '+data['reason'],ddk=True)
            if job.version==candidate.version:raise ValidationError('Новые параметры совпадают с заданием.')
            report=job.reports.filter(kind='plan_change',author=actor).first()
            report.token=data['token'];report.request_digest=digest;report.performed_date=data['date']
            report.context={**report.context,'ddk':True};report.save(update_fields=['token','request_digest','performed_date','context'])
            for upload in uploads:
                key=store_file(upload);stored.append(key)
                Attachment.objects.create(report=report,storage_key=key,**{k:upload[k] for k in ('name','content_type','size','sha256')})
            return report
    except Exception:
        for key in stored:private_path(key).unlink(missing_ok=True)
        raise


@login_required
def revise(request,pk):
    job=get_object_or_404(allowed(request.user),pk=pk)
    form=DDKForm(request.POST or None,request.FILES or None,initial={**{k:getattr(job,k) for k in plans.FIELDS},'expected_version':job.version,'token':uuid.uuid4(),'date':timezone.localdate()})
    if request.method=='POST' and form.is_valid():
        try:change(request.user,job.pk,form.cleaned_data)
        except ValidationError as error:form.add_error(None,error)
        else:
            messages.success(request,'Метод после ДДК сохранён. Исходное задание и основание — в истории. Проверьте новую потребность и закрепление муфт; результат монтажа вносится отдельно.')
            return redirect('repair_detail',pk=job.pk)
    return render(request,'repairs/ddk.html',{'form':form,'job':job})
