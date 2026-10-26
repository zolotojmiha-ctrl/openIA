from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render,redirect,get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
from .models import Preference,Reminder,Favorite,SupportRequest,UserSession,User,PERMISSIONS
from .views import audit
from documents.access import visible_documents,visible_bundles,has_permission
from projects.access import visible_objects,visible_contracts

class PreferenceForm(forms.ModelForm):
    class Meta:
        model=Preference
        fields=['hints','notify_documents','notify_versions','notify_comments','large_text','compact']

class IdentityForm(forms.Form):
    snapshot=forms.CharField(widget=forms.HiddenInput)
    full_name=forms.CharField(label='Фамилия, имя и отчество',max_length=150)

@login_required
def profile(request):
    obj,_=Preference.objects.get_or_create(user=request.user)
    identity_post=request.method=='POST' and request.POST.get('action')=='identity'
    name_form=IdentityForm(request.POST if identity_post else None,initial={'full_name':request.user.full_name,'snapshot':signing.dumps([str(request.user.pk),request.user.full_name],salt='own-profile-name')})
    form=PreferenceForm(request.POST if request.method=='POST' and not identity_post else None,instance=obj)
    if identity_post and name_form.is_valid():
        try:
            shown=signing.loads(name_form.cleaned_data['snapshot'],salt='own-profile-name',max_age=3600)
            with transaction.atomic():
                actor=User.objects.select_for_update().get(pk=request.user.pk)
                if not actor.is_active or actor.session_version!=request.user.session_version:raise PermissionDenied
                if shown!=[str(actor.pk),actor.full_name]:raise ValidationError('Имя уже изменилось или форма устарела. Обновите страницу.')
                before=actor.full_name;actor.full_name=name_form.cleaned_data['full_name']
                actor.save(update_fields=['full_name'])
                if before!=actor.full_name:audit(actor,'employee_updated',actor,kind='own_profile_name',previous_name=before,full_name=actor.full_name)
        except (signing.BadSignature,ValidationError) as exc:name_form.add_error(None,exc if isinstance(exc,ValidationError) else 'Форма устарела. Обновите страницу.')
        else:messages.success(request,'ФИО сохранено.');return redirect('profile')
    elif request.method=='POST' and not identity_post and form.is_valid():form.save();return redirect('profile')
    keys=Session.objects.filter(expire_date__gt=timezone.now()).values('session_key')
    sessions=request.user.active_sessions.filter(session_key__in=keys).order_by('-last_seen')
    return render(request,'accounts/profile.html',{'form':form,'name_form':name_form,'sessions':sessions,'current_key':request.session.session_key,'permissions':request.user.grants.filter(permission__in=PERMISSIONS)})

@login_required
@require_POST
def end_sessions(request):
    if request.POST.get('all')=='1':
        from django.contrib.auth import update_session_auth_hash
        request.user.session_version+=1;request.user.save(update_fields=['session_version']);update_session_auth_hash(request,request.user)
        keys=request.user.active_sessions.exclude(session_key=request.session.session_key).values_list('session_key',flat=True)
        Session.objects.filter(session_key__in=keys).delete();request.user.active_sessions.exclude(session_key=request.session.session_key).delete()
    else:
        record=get_object_or_404(UserSession,user=request.user,pk=request.POST.get('session'))
        if record.session_key!=request.session.session_key:Session.objects.filter(session_key=record.session_key).delete();record.delete()
    audit(request.user,'sessions_ended');return redirect('profile')

class ReminderForm(forms.ModelForm):
    class Meta:
        model=Reminder
        fields=['date','text','done']
        widgets={'date':forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'})}

@login_required
def reminders(request,pk=None):
    obj=get_object_or_404(Reminder,user=request.user,pk=pk) if pk else None
    form=ReminderForm(request.POST or None,instance=obj)
    doc=None
    if request.GET.get('document'):
        from uuid import UUID
        try:key=UUID(request.GET['document'])
        except ValueError:raise PermissionDenied
        doc=get_object_or_404(visible_documents(request.user),pk=key)
    if request.method=='POST' and form.is_valid():
        saved=form.save(commit=False);saved.user=request.user
        if doc:saved.document=doc
        saved.save();return redirect('reminders')
    items=Reminder.objects.filter(user=request.user).filter(document__isnull=True)|Reminder.objects.filter(user=request.user,document__in=visible_documents(request.user))
    return render(request,'accounts/reminders.html',{'form':form,'items':items.order_by('done','date'),'document':doc})


def favorite_entity(user,kind,pk):
    choices={'document':(visible_documents(user),'document_detail'),'bundle':(visible_bundles(user),'bundle_detail'),'object':(visible_objects(user),'object_detail'),'contract':(visible_contracts(user),'contract_detail')}
    if kind not in choices:raise PermissionDenied
    qs,route=choices[kind];obj=qs.filter(pk=pk).first()
    if not obj:return None
    from django.urls import reverse
    title=getattr(obj,'title',None) or getattr(obj,'name',None)
    if kind=='contract':
        from projects.access import contract_card
        title=contract_card(obj,user)['title']
    return {'title':title,'url':reverse(route,args=[obj.pk])}

def favorite_items(user):
    result=[]
    for row in Favorite.objects.filter(user=user):
        obj=favorite_entity(user,row.kind,row.entity_id)
        if obj:result.append(obj)
    return result

@login_required
@require_POST
def favorite(request,kind,pk):
    if not favorite_entity(request.user,kind,pk):raise PermissionDenied
    with transaction.atomic():
        User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
        row=Favorite.objects.filter(user=request.user,kind=kind,entity_id=pk).first()
        if row:row.delete()
        else:Favorite.objects.create(user=request.user,kind=kind,entity_id=pk)
    return redirect(favorite_entity(request.user,kind,pk)['url'])

class RequestForm(forms.ModelForm):
    class Meta:
        model=SupportRequest
        fields=['text']

class ResponseForm(forms.ModelForm):
    class Meta:
        model=SupportRequest
        fields=['status','response']

@login_required
def support(request,pk=None):
    qs=SupportRequest.objects.all() if request.user.is_leader else SupportRequest.objects.filter(author=request.user)
    obj=get_object_or_404(qs,pk=pk) if pk else None
    if obj and not request.user.is_leader:return render(request,'accounts/support.html',{'items':[obj]})
    form=ResponseForm(request.POST or None,instance=obj) if obj else RequestForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        saved=form.save(commit=False)
        if not obj:saved.author=request.user
        saved.save();audit(request.user,'support_updated',request_id=saved.pk,reply=bool(obj))
        messages.success(request,'Ответ сохранён. Автор увидит уведомление.' if obj else 'Обращение сохранено. Оба руководителя увидят его в уведомлениях.')
        return redirect('support')
    return render(request,'accounts/support.html',{'form':form,'items':qs.select_related('author').order_by('-created_at')[:100]})


class TransferForm(forms.Form):
    recipient=forms.ModelChoiceField(label='Новый ответственный',queryset=User.objects.none())
    documents=forms.ModelMultipleChoiceField(label='Выбранные документы — можно отметить все',queryset=None,widget=forms.CheckboxSelectMultiple)
    def __init__(self,*args,source,**kwargs):
        from django.db.models import Q
        from documents.models import Document
        super().__init__(*args,**kwargs)
        self.fields['recipient'].queryset=User.objects.filter(is_active=True).exclude(pk=source.pk)
        self.fields['recipient'].label_from_instance=lambda u:u.full_name
        self.fields['documents'].queryset=Document.objects.filter(Q(created_by=source)|Q(responsible=source),legal_entity__tax_id='8604073687')

@login_required
def transfer(request,pk):
    if not request.user.is_leader:raise PermissionDenied
    source=get_object_or_404(User,pk=pk)
    form=TransferForm(request.POST or None,source=source)
    if request.method=='POST' and form.is_valid():
        from documents.models import Document
        with transaction.atomic():
            for doc in Document.objects.select_for_update().filter(pk__in=[d.pk for d in form.cleaned_data['documents']]).order_by('pk'):
                doc.responsible=form.cleaned_data['recipient'];doc.revision+=1;doc.save(update_fields=['responsible','revision'])
                audit(request.user,'document_responsibility_changed',entity_id=str(doc.pk),previous_responsible=str(source.pk),responsible=str(doc.responsible_id))
        return redirect('users')
    return render(request,'accounts/transfer.html',{'form':form,'source':source})

@login_required
def notifications(request):
    from django.http import JsonResponse
    from documents.access import unread_documents,unsorted_documents,visible_documents
    from documents.models import Notice
    p,_=Preference.objects.get_or_create(user=request.user)
    unread=unread_documents(request.user).select_related('current_version').order_by('-current_version__created_at')
    candidates=unread.filter(current_version__sequence=1) if p.notify_documents and not p.notify_versions else unread.filter(current_version__sequence__gt=1) if p.notify_versions and not p.notify_documents else unread if p.notify_versions and p.notify_documents else unread.none()
    keys=['v:'+str(doc.current_version_id) for doc in candidates[:50]]
    if p.notify_comments:keys+=['c:'+str(pk) for pk in Notice.objects.filter(user=request.user,read=False,document__in=visible_documents(request.user)).values_list('pk',flat=True)[:50]]
    from .business_notices import unread as business_unread
    business=business_unread(request.user)
    business_count=business.count()
    keys+=['b:'+str(pk) for pk in business.values_list('pk',flat=True)[:50]]
    from postoffice.access import boxes
    mail_rows=list(boxes(request.user).filter(state='active',unread__gt=0).values('pk','unread','unread_digest'))
    old_keys=list(keys)
    keys+=['m:'+str(b['pk'])+':'+b['unread_digest'] for b in mail_rows]
    new=keys
    from django.urls import reverse
    notices=Notice.objects.filter(user=request.user,read=False,document__in=visible_documents(request.user)).order_by('-created_at')
    url=reverse('document_detail',args=[notices[0].document_id]) if not unread.exists() and notices.exists() else reverse('documents')+'?mode=inbox'
    if business_count:url=reverse('business_inbox')
    elif mail_rows and not old_keys:url=reverse('mail')
    from postoffice.access import boxes
    mail_count=sum(boxes(request.user).filter(state='active').values_list('unread',flat=True))
    return JsonResponse({'mailboxes':{str(b['pk']):b['unread'] for b in boxes(request.user).filter(state='active').values('pk','unread')},'mail':mail_count,'business':business_count,'identity':str(request.user.pk),'keys':keys,'url':url,'inbox':unread.count(),'unsorted':unsorted_documents(request.user).count(),'notification':'Новые уведомления: '+str(len(new)) if new else ''})

@login_required
def business_inbox(request):
    from django.core.paginator import Paginator
    from .business_notices import events,destination,notice_title
    from .models import BusinessNoticeRead
    qs=events(request.user)
    if request.method=='POST':
        # Only explicitly displayed rows are acknowledged, never unseen new events.
        ids=request.POST.getlist('events')[:100]
        if any(not i.isdigit() for i in ids):raise PermissionDenied
        allowed=qs.filter(pk__in=ids)
        BusinessNoticeRead.objects.bulk_create([BusinessNoticeRead(user=request.user,event=e) for e in allowed],ignore_conflicts=True)
        return redirect('business_inbox')
    if request.GET.get('all')!='1':qs=qs.exclude(pk__in=BusinessNoticeRead.objects.filter(user=request.user).values('event_id'))
    page=Paginator(qs,50).get_page(request.GET.get('page'))
    return render(request,'accounts/business_inbox.html',{'page':page,'notice_rows':[{'event':e,'url':destination(e),'title':notice_title(e)} for e in page],'show_all':request.GET.get('all')=='1'})
