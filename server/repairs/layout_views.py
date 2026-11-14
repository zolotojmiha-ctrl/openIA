from django import forms
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied,ValidationError
from django.contrib import messages
from django.shortcuts import render,redirect,get_object_or_404
from django.views.decorators.http import require_POST
from .access import can_prepare,visible_sections
from .models import PipeLayout
from . import layouts

class Upload(forms.Form):
    file=forms.FileField(label='Раскладка секций XLSX')
class Confirm(forms.Form):
    token=forms.CharField(widget=forms.HiddenInput)
    confirm=forms.BooleanField(label='Использовать эту раскладку для расчёта расстояний')

def token(user,row):return signing.dumps({'user':str(user.pk),'session':user.session_version,'layout':str(row.pk),'sha':row.sha256},salt='pipe-layout')

@login_required
def upload(request,section_id):
    if not layouts.manage_allowed(request.user):raise PermissionDenied
    section=get_object_or_404(visible_sections(request.user),pk=section_id)
    active=PipeLayout.objects.filter(section=section,state='active').first()
    form=Upload(request.POST or None,request.FILES or None)
    if request.method=='POST' and form.is_valid():
        try:row=layouts.prepare(request.user,section.pk,form.cleaned_data['file'])
        except ValidationError as e:form.add_error(None,e)
        else:return redirect('repair_layout_review',pk=row.pk)
    return render(request,'repairs/layout_upload.html',{'form':form,'section':section,'active':active,'history':PipeLayout.objects.filter(section=section,state__in=['active','superseded']).order_by('-applied_at')[:10]})

@login_required
def review(request,pk):
    if not layouts.manage_allowed(request.user):raise PermissionDenied
    row=layouts.scoped(request.user,pk)
    if not row or row.state=='preview' and row.author_id!=request.user.pk:raise PermissionDenied
    form=Confirm(request.POST or None,initial={'token':token(request.user,row)})
    if request.method=='POST' and form.is_valid():
        try:
            data=signing.loads(form.cleaned_data['token'],salt='pipe-layout',max_age=86400)
            if data!={'user':str(request.user.pk),'session':request.user.session_version,'layout':str(row.pk),'sha':row.sha256}:raise signing.BadSignature
            layouts.apply(request.user,row.pk)
        except signing.BadSignature:form.add_error(None,'Подтверждение устарело. Повторите предпросмотр.')
        except ValidationError as e:form.add_error(None,e)
        else:
            messages.success(request,'Раскладка выбрана. Дефекты, методы ремонта и складские остатки сохранены.')
            return redirect('repair_layout_upload',section_id=row.section_id)
    return render(request,'repairs/layout_review.html',{'layout':row,'form':form,'stats':layouts.status(row),'sample':row.data['rows'][:5]})

@login_required
@require_POST
def cancel(request,pk):
    row=layouts.scoped(request.user,pk)
    if not row:raise PermissionDenied
    state=layouts.cancel(request.user,pk)
    messages.info(request,'Предпросмотр отменён.' if state=='cancelled' else 'Раскладка уже применена. Действующая версия сохранена.')
    return redirect('repair_layout_upload',section_id=row.section_id)


@login_required
def sections(request):
    if not layouts.manage_allowed(request.user):raise PermissionDenied
    from projects.scope_widgets import identifiers
    from django.core.paginator import Paginator
    rows=visible_sections(request.user).select_related('project_object').order_by('project_object__name','pipeline','pk')
    if request.GET.get('contract'):
        try:rows=rows.filter(project_object__contracts__in=identifiers(request.GET,'contract')).distinct()
        except ValueError:rows=rows.none()
    return render(request,'repairs/layout_sections.html',{'page':Paginator(rows,25).get_page(request.GET.get('page')),'contract':request.GET.get('contract','')})
