"""Scoped photo edits use server-side previews shared with native MAX."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError,PermissionDenied
from django.http import Http404,FileResponse
from django.shortcuts import render,redirect,get_object_or_404
from documents.storage import verified_file
from . import photos
from .access import visible_jobs
from .models import Attachment,PhotoOperation,PhotoRevision


class EditForm(forms.Form):
    name=forms.CharField(label='Название',max_length=200)
    caption=forms.CharField(label='Подпись',max_length=1000,required=False,widget=forms.Textarea(attrs={'rows':3}))
    file=forms.FileField(label='Заменить фотографию — необязательно',required=False,widget=forms.ClearableFileInput(attrs={'accept':'image/png,image/jpeg'}))


@login_required
def edit(request,pk,action):
    photo=get_object_or_404(photos.accessible(request.user),pk=pk)
    photos._authorize(request.user,photo,action)
    form=EditForm(request.POST or None,request.FILES or None,initial={'name':photo.name,'caption':photo.caption}) if action=='edit' else None
    if request.method=='POST' and (form is None or form.is_valid()):
        try:
            data=form.cleaned_data if form else {}
            item=photos.preview(request.user,pk,action,name=data.get('name',''),caption=data.get('caption',''),upload=data.get('file'))
            return redirect('repair_photo_review',pk=item.pk)
        except ValidationError as error:
            if form:form.add_error(None,error)
            else:messages.error(request,' '.join(error.messages))
    return render(request,'repairs/photo_edit.html',{'photo':photo,'form':form,'action':action})


@login_required
def review(request,pk):
    item=get_object_or_404(PhotoOperation,pk=pk,actor=request.user)
    if request.method=='POST':
        try:
            if request.POST.get('decision')=='cancel':photos.cancel(request.user,pk)
            elif request.POST.get('decision')=='confirm':photos.confirm(request.user,pk);messages.success(request,'Изменение фотографии сохранено.')
            else:raise ValidationError('Выберите подтверждение или отмену.')
            return redirect('repair_detail',pk=item.photo.report.job_id)
        except ValidationError as error:messages.error(request,' '.join(error.messages))
    item=photos.operation(request.user,pk)
    return render(request,'repairs/photo_review.html',{'item':item,'photo':item.photo})


@login_required
def history(request,pk):
    photo=get_object_or_404(Attachment.objects.filter(report__job__in=visible_jobs(request.user)),pk=pk)
    from .models import PhotoPurge
    keys=[manifest.get('storage_key','') for manifest in photo.versions.values_list('manifest',flat=True)]
    purges=PhotoPurge.objects.filter(storage_key__in=keys)
    return render(request,'repairs/photo_history.html',{'purges':purges,'photo':photo,'versions':photo.versions.select_related('actor').order_by('-revision')})


@login_required
def version(request,pk):
    item=get_object_or_404(PhotoRevision.objects.filter(photo__report__job__in=visible_jobs(request.user),photo__deleted_at__isnull=True).exclude(storage_key=''),pk=pk)
    try:content=verified_file(item)
    except (OSError,ValueError):raise Http404('Версия недоступна.')
    response=FileResponse(content,content_type=item.manifest['content_type'])
    response['Content-Security-Policy']="default-src 'none'; sandbox";response['Cache-Control']='private, no-store'
    return response
