import uuid,hashlib
from urllib.parse import urlencode
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError,PermissionDenied
from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.shortcuts import render,redirect,get_object_or_404
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters,sensitive_variables
from accounts.views import audit
from .access import boxes,leader
from .models import Mailbox,Letter
from .forms import RequestForm,ConnectForm,ComposeForm
from . import services,transport

@login_required
def home(request):
    return render(request,'postoffice/home.html',{'boxes':boxes(request.user).order_by('address')})

@login_required
@sensitive_post_parameters()
@sensitive_variables('form')
def configure(request,pk=None):
    leader(request.user)
    box=get_object_or_404(boxes(request.user),pk=pk) if pk else None
    initial={'address':box.address,'owner':box.owner_id,'person':box.person_id,'version':box.version,'enabled':box.state=='active'} if box else {'address':request.GET.get('address',''),'owner':request.GET.get('owner'),'person':request.GET.get('person')}
    form=(ConnectForm if box else RequestForm)(request.POST or None,initial=initial)
    if request.method=='POST' and form.is_valid():
        c=form.cleaned_data
        try:
            if box:services.configure(request.user,box.pk,c['version'],c['password'],c['owner'],c['person'],c['enabled'])
            else:box=services.request_address(request.user,c['address'],c['owner'],c['person'])
        except ValidationError as e:form.add_error(None,e)
        else:messages.success(request,'Настройки сохранены.' if pk else 'Адрес добавлен как заявка. Сам почтовый ящик ещё нужно создать у провайдера.');return redirect('mailbox_configure',pk=box.pk)
    return render(request,'postoffice/configure.html',{'form':form,'box':box})

@login_required
def inbox(request,pk):
    box=get_object_or_404(boxes(request.user),pk=pk)
    folder=request.GET.get('folder','INBOX');error='';rows=[];all_folders=[];total=0
    try:page=max(1,min(int(request.GET.get('page','1')),100000))
    except ValueError:page=1
    query=request.GET.get('q','').strip()[:150]
    if box.state=='active':
        try:
            with transport.connect(box) as client:
                all_folders=transport.folders(client);rows,total=transport.listing(client,folder,page,query)
                transport.select(client,'INBOX');unread=transport.checked(client.uid('SEARCH',None,'UNSEEN','UNDELETED'))[0].split()
                Mailbox.objects.filter(pk=box.pk,version=box.version).update(unread=len(unread),unread_digest=hashlib.sha256(b' '.join(unread)).hexdigest(),synced_at=timezone.now())
            audit(request.user,'mail_read',mailbox=str(box.pk),kind='folder')
        except ValidationError as e:error='; '.join(e.messages)
    return render(request,'postoffice/inbox.html',{'box':box,'rows':rows,'folders':all_folders,'folder':folder,'page_number':page,'has_next':page*25<total,'previous':page-1,'next':page+1,'query':query,'error':error})

@login_required
def detail(request,pk,uid):
    box=get_object_or_404(boxes(request.user),pk=pk);folder=request.GET.get('folder','INBOX')
    try:
        with transport.connect(box) as client:letter=transport.message(client,folder,uid)
        audit(request.user,'mail_read',mailbox=str(box.pk),kind='message',uid=uid)
    except ValidationError as e:messages.error(request,'; '.join(e.messages));return redirect(reverse('mail_inbox',args=[pk])+'?'+urlencode({'folder':folder}))
    return render(request,'postoffice/detail.html',{'box':box,'letter':letter,'folder':folder,'uid':uid})

@login_required
def attachment(request,pk,uid,index):
    box=get_object_or_404(boxes(request.user),pk=pk)
    try:
        with transport.connect(box) as client:letter=transport.message(client,request.GET.get('folder','INBOX'),uid)
    except ValidationError as e:
        messages.error(request,'; '.join(e.messages));return redirect(reverse('mail_inbox',args=[pk])+'?'+urlencode({'folder':request.GET.get('folder','INBOX')}))
    part=next((p for p in letter['attachments'] if p['index']==index),None)
    if part is None:raise PermissionDenied
    content=part['part'].get_payload(decode=True)
    if content is None or len(content)>transport.MAX_MESSAGE:raise PermissionDenied
    from django.utils.http import content_disposition_header
    response=HttpResponse(content,content_type='application/octet-stream');response['Content-Disposition']=content_disposition_header(True,part['name'].replace('\r','').replace('\n',''));response['X-Content-Type-Options']='nosniff'
    audit(request.user,'mail_read',mailbox=str(box.pk),kind='attachment',uid=uid,index=index)
    return response

@login_required
def compose(request,pk,letter_id=None):
    box=get_object_or_404(boxes(request.user),pk=pk)
    if box.state!='active':messages.error(request,'Сначала подключите почтовый ящик.');return redirect('mail_inbox',pk=pk)
    draft=get_object_or_404(Letter,pk=letter_id,mailbox=box,author=request.user,state='draft') if letter_id else None
    initial={k:getattr(draft,k) for k in ('to','cc','bcc','subject','body')} if draft else {}
    initial.update(letter_id=draft.pk if draft else uuid.uuid4(),version=draft.version if draft else 0)
    if request.method=='GET' and request.GET.get('reply'):
        try:
            with transport.connect(box) as client:original=transport.message(client,request.GET.get('folder','INBOX'),request.GET['reply'])
            initial['source']=signing.dumps({'actor':str(request.user.pk),'box':str(box.pk),'folder':request.GET.get('folder','INBOX'),'uid':request.GET['reply'],'fingerprint':original.get('fingerprint',''),'message_id':original.get('message_id',''),'forward':request.GET.get('forward')=='1'},salt='mail-source')
            initial['forward_attachments']=request.GET.get('forward')=='1'
            if request.GET.get('forward')=='1':initial.update(subject='Fwd: '+original['subject'][:240],body='\n\nПересланное письмо от '+original['sender']+'\n'+original['body'][:22000])
            else:initial.update(to=original['reply_to'],subject='Re: '+original['subject'][:240],body='\n\n'+ '\n'.join('> '+line for line in original['body'][:22000].splitlines())[:23000])
        except ValidationError as e:messages.error(request,'; '.join(e.messages))
    forward_mode=bool(initial.get('forward_attachments'))
    if request.method=='POST' and request.POST.get('source'):
        try:forward_mode=bool(signing.loads(request.POST['source'],salt='mail-source',max_age=3600).get('forward'))
        except signing.BadSignature:pass
    form=ComposeForm(request.POST or None,initial=initial,forward_mode=forward_mode)
    if request.method=='POST' and form.is_valid():
        c=form.cleaned_data
        try:
            uploads=request.FILES.getlist('files')
            if c.get('source'):
                source=signing.loads(c['source'],salt='mail-source',max_age=3600)
                if source['actor']!=str(request.user.pk) or source['box']!=str(box.pk):raise PermissionDenied
                c['_reply_id']=source['message_id'] if not source['forward'] else ''
                if source['forward'] and c['forward_attachments']:
                    with transport.connect(box) as client:original=transport.message(client,source['folder'],source['uid'])
                    if original['fingerprint']!=source['fingerprint']:raise ValidationError('Исходное письмо изменилось. Откройте пересылку заново.')
                    uploads += [SimpleUploadedFile(a['name'],a['part'].get_payload(decode=True) or b'') for a in original['attachments']]
            row=services.save_letter(request.user,box.pk,c['letter_id'],c['version'],c,uploads,queue=request.POST.get('action')=='send')
        except signing.BadSignature:form.add_error(None,'Исходное письмо устарело. Откройте ответ или пересылку заново.')
        except ValidationError as e:form.add_error(None,e)
        else:
            messages.success(request,'Письмо поставлено в очередь. Следите за состоянием в исходящих.' if row.state=='queued' else 'Черновик сохранён.')
            return redirect('mail_outbox',pk=box.pk)
    return render(request,'postoffice/compose.html',{'box':box,'form':form,'draft':draft})

@login_required
def outbox(request,pk):
    box=get_object_or_404(boxes(request.user),pk=pk)
    qs=Letter.objects.filter(mailbox=box).order_by('-created_at')
    if not request.user.is_leader:qs=qs.filter(author=request.user)
    return render(request,'postoffice/outbox.html',{'box':box,'letters':qs[:100]})

@login_required
def local_letter(request,pk,letter_id,attachment_id=None):
    box=get_object_or_404(boxes(request.user),pk=pk)
    row=get_object_or_404(Letter,pk=letter_id,mailbox=box)
    if not request.user.is_leader and row.author_id!=request.user.pk:raise PermissionDenied
    audit(request.user,'mail_read',mailbox=str(box.pk),kind='local_letter',letter=str(row.pk))
    if attachment_id is not None:
        file=get_object_or_404(row.attachments,pk=attachment_id)
        from django.utils.http import content_disposition_header
        response=HttpResponse(bytes(file.data),content_type='application/octet-stream');response['Content-Disposition']=content_disposition_header(True,file.name.replace('\r','').replace('\n',''));return response
    return render(request,'postoffice/local_letter.html',{'box':box,'letter':row})

@login_required
def message_action(request,pk,uid):
    if request.method!='POST':return HttpResponse(status=405)
    box=get_object_or_404(boxes(request.user),pk=pk);folder=request.POST.get('folder','INBOX')
    try:
        with transport.connect(box) as client:
            transport.select(client,folder,readonly=False)
            action=request.POST.get('action')
            if action=='seen':transport.checked(client.uid('STORE',str(uid),'+FLAGS','(\\Seen)'))
            elif action in ('trash','restore'):
                target='INBOX' if action=='restore' else next((f['name'] for f in transport.folders(client) if f['trash']),None)
                if not target or target==folder:raise ValidationError('Папка назначения не найдена или письмо уже в ней.')
                target='"'+target.replace('\\','\\\\').replace('"','\\"')+'"'
                if b'MOVE' in client.capabilities:transport.checked(client.uid('MOVE',str(uid),target))
                else:
                    transport.checked(client.uid('COPY',str(uid),target));transport.checked(client.uid('STORE',str(uid),'+FLAGS','(\\Deleted)'))
            else:raise ValidationError('Неизвестное действие.')
        audit(request.user,'mail_prepared',mailbox=str(box.pk),kind=action,uid=uid)
    except ValidationError as e:messages.error(request,'; '.join(e.messages))
    return redirect(reverse('mail_inbox',args=[pk])+'?'+urlencode({'folder':folder}))
