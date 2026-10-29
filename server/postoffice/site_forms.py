"""Anonymous, same-origin intake. Only fixed company recipients are allowed."""
import hashlib
import io
import ipaddress
import json
import re
import uuid
import zipfile
from datetime import timedelta
from functools import wraps
from django import forms
from django.conf import settings
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.http import FileResponse, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST
from .access import boxes
from .models import SiteRequest, SiteRequestAttempt, Mailbox

RECIPIENT='info@armada-sibir.ru'
CONSENT_VERSION='2026-10-30'
SALT='armada-public-request-v1'
MAX_RESUME=5*1024*1024
KINDS=dict(SiteRequest.KINDS)
LABELS={'name':'Имя','phone':'Телефон','email':'Электронная почта','city':'Город','profession':'Профессия / должность','experience':'Опыт работы','travel':'Готовность к командировкам / вахте','about':'О себе','company':'Компания','type':'Изделие','dn':'Условный диаметр DN','quantity':'Количество','region':'Место поставки','details':'Требования','equipment':'Техника / оборудование'}


class IntakeForm(forms.Form):
    name=forms.CharField(max_length=120,required=False)
    phone=forms.CharField(max_length=120)
    email=forms.EmailField(max_length=254,required=False)
    city=forms.CharField(max_length=120,required=False)
    profession=forms.CharField(max_length=120,required=False)
    experience=forms.CharField(max_length=3000,required=False)
    travel=forms.ChoiceField(choices=[('',''),('Готов(а)','Готов(а)'),('Готов(а) обсудить условия','Готов(а) обсудить условия'),('Не готов(а)','Не готов(а)')],required=False)
    about=forms.CharField(max_length=3000,required=False)
    company=forms.CharField(max_length=120,required=False)
    type=forms.CharField(max_length=120,required=False)
    dn=forms.CharField(max_length=80,required=False)
    quantity=forms.CharField(max_length=80,required=False)
    region=forms.CharField(max_length=120,required=False)
    details=forms.CharField(max_length=3000,required=False)
    equipment=forms.CharField(max_length=200,required=False)
    consent=forms.BooleanField(required=True,error_messages={'required':'Подтвердите согласие на обработку данных.'})
    resume=forms.FileField(required=False)

    def __init__(self,*args,kind,**kwargs):
        super().__init__(*args,**kwargs)
        allowed={'candidate':('name','phone','email','city','profession','experience','travel','about','consent','resume'),
                 'rental':('phone','equipment','consent'),
                 'couplings':('company','name','phone','email','type','dn','quantity','region','details','consent'),
                 'centralizers':('company','name','phone','email','type','dn','quantity','region','details','consent')}[kind]
        self.fields={name:field for name,field in self.fields.items() if name in allowed}
        for name,field in self.fields.items():
            field.label=LABELS.get(name,{'consent':'Согласие','resume':'Резюме'}.get(name,name))
        required={'candidate':('name','email','city','profession','experience'), 'rental':(),
                  'couplings':('name','email','dn','quantity'), 'centralizers':('name','email','dn','quantity')}[kind]
        for name in required:self.fields[name].required=True
        if kind=='couplings':self.fields['type']=forms.ChoiceField(label='Изделие',choices=[(x,x) for x in ('П1','П2','П1 и П2')])
        if kind=='centralizers':self.fields['type']=forms.ChoiceField(label='Изделие',choices=[('Наружный звеньевой центратор','Наружный звеньевой центратор')])

    def clean_phone(self):
        value=self.cleaned_data['phone']
        if not re.fullmatch(r'[+\d\s().-]+',value) or not 7<=len(re.sub(r'\D','',value))<=15:
            raise forms.ValidationError('Укажите телефон с кодом города или страны.')
        return value

    def clean_resume(self):
        file=self.cleaned_data.get('resume')
        if not file:return None
        if file.size<=0 or file.size>MAX_RESUME:raise forms.ValidationError('Прикрепите непустое резюме размером до 5 МБ.')
        data=file.read(MAX_RESUME+1)
        name=file.name.replace('\\','/').split('/')[-1].replace('\r','').replace('\n','')[:200]
        if len(data)>MAX_RESUME:raise forms.ValidationError('Резюме должно быть не больше 5 МБ.')
        if name.lower().endswith('.pdf') and data.startswith(b'%PDF-'):
            mime='application/pdf'
        elif name.lower().endswith('.docx') and data.startswith(b'PK'):
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    entries=archive.infolist()
                    if len(entries)>1000 or sum(e.file_size for e in entries)>30*1024*1024 or '[Content_Types].xml' not in archive.namelist() or 'word/document.xml' not in archive.namelist():raise ValueError
                    if any(e.filename.lower().endswith('vbaproject.bin') for e in entries):raise ValueError
            except (zipfile.BadZipFile,ValueError,OSError):raise forms.ValidationError('Не удалось прочитать DOCX. Прикрепите обычный документ Word или PDF.') from None
            mime='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        else:raise forms.ValidationError('Прикрепите резюме в формате PDF или DOCX.')
        return {'name':name,'data':data,'type':mime}


def can_read(user):
    return bool(user.is_authenticated and user.is_active and user.role!='customer' and
                (user.is_leader or boxes(user).filter(address__iexact=RECIPIENT).exists()))


def staff(view):
    @login_required
    @wraps(view)
    def wrapped(request,*args,**kwargs):
        if not can_read(request.user):raise PermissionDenied
        return view(request,*args,**kwargs)
    return wrapped


def client_key(request):
    address=request.META.get('REMOTE_ADDR','')
    if settings.SECURE_PROXY_SSL_HEADER and request.META.get('HTTP_X_FORWARDED_FOR'):
        address=request.META['HTTP_X_FORWARDED_FOR'].split(',')[-1].strip()
    try:address=str(ipaddress.ip_address(address))
    except ValueError:address='unknown'
    return salted_hmac('armada-public-request-ip',address).hexdigest()


@never_cache
@require_GET
def token(request):
    kind=request.GET.get('kind','')
    if kind not in KINDS:return JsonResponse({'ok':False},status=404)
    return JsonResponse({'csrf':get_token(request),'token':signing.dumps({'id':str(uuid.uuid4()),'kind':kind},salt=SALT)})


def failure(text,status=400,field='__all__'):
    return JsonResponse({'ok':False,'errors':{field:[text]}},status=status)


@never_cache
@require_POST
@sensitive_post_parameters()
def submit(request,kind):
    if kind not in KINDS:return failure('Форма не найдена.',404)
    if kind!='candidate' and request.FILES:return failure('В этой форме вложения не предусмотрены.')
    try:
        signed=signing.loads(request.POST.get('token',''),salt=SALT,max_age=24*3600)
        if signed.get('kind')!=kind:raise ValueError
        request_id=uuid.UUID(signed['id'])
    except (signing.BadSignature,ValueError,KeyError,TypeError):return failure('Срок формы истёк. Обновите страницу и повторите отправку.')
    if request.POST.get('website','').strip():return failure('Не удалось принять форму. Обновите страницу и повторите отправку.')
    form=IntakeForm(request.POST,request.FILES,kind=kind)
    if not form.is_valid():
        return JsonResponse({'ok':False,'errors':{name:[f'{form.fields[name].label}: {message}' for message in errors] for name,errors in form.errors.items()}},status=400)
    if len(request.FILES)>int('resume' in request.FILES) or len(request.FILES.getlist('resume'))>1:return failure('К анкете можно приложить одно резюме.',field='resume')
    cleaned=form.cleaned_data.copy();resume=cleaned.pop('resume',None);cleaned.pop('consent',None)
    digest=hashlib.sha256(json.dumps([kind,cleaned,resume['name'] if resume else '',hashlib.sha256(resume['data']).hexdigest() if resume else '',CONSENT_VERSION],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with transaction.atomic():
        rate,_=SiteRequestAttempt.objects.get_or_create(key=client_key(request))
        rate=SiteRequestAttempt.objects.select_for_update().get(pk=rate.pk)
        previous=SiteRequest.objects.filter(pk=request_id).only('digest').first()
        if previous:
            if previous.digest!=digest:return failure('Эта отправка уже сохранена с другими данными. Обновите форму для нового обращения.',409)
        else:
            now=timezone.now()
            if now-rate.window_start>=timedelta(hours=1):rate.window_start=now;rate.count=0
            rate.count+=1;rate.save(update_fields=['count','window_start'])
            if rate.count>10:return failure('Слишком много обращений. Попробуйте позже или свяжитесь с нами по телефону.',429)
            try:
                with transaction.atomic():
                    SiteRequest.objects.create(id=request_id,kind=kind,data=cleaned,digest=digest,consent_version=CONSENT_VERSION,
                        resume_name=resume['name'] if resume else '',resume_type=resume['type'] if resume else '',resume_data=resume['data'] if resume else b'')
            except IntegrityError:
                if SiteRequest.objects.only('digest').get(pk=request_id).digest!=digest:return failure('Эта отправка уже сохранена с другими данными. Обновите форму для нового обращения.',409)
    message='Спасибо! Анкета принята и передана на рассмотрение.' if kind=='candidate' else 'Спасибо! Запрос принят. Мы свяжемся с вами по указанным контактам.'
    return JsonResponse({'ok':True,'message':message,'reference':request_id.hex[:12]})


def visible_requests(user):
    if not can_read(user):raise PermissionDenied
    return SiteRequest.objects.defer('resume_data')


@staff
def listing(request):
    query=visible_requests(request.user)
    kind=request.GET.get('kind','')
    if kind in KINDS:query=query.filter(kind=kind)
    if request.GET.get('unread')=='1':query=query.exclude(read_by=request.user)
    return render(request,'postoffice/site_requests.html',{'page':Paginator(query,25).get_page(request.GET.get('page')),
        'request_kinds':SiteRequest.KINDS,'selected_kind':kind,'unread_only':request.GET.get('unread')=='1',
        'info_connected':Mailbox.objects.filter(address__iexact=RECIPIENT,state='active').exclude(credential='').exists()})


@staff
def detail(request,pk):
    row=get_object_or_404(visible_requests(request.user),pk=pk)
    if not row.read_by.filter(pk=request.user.pk).exists():
        row.read_by.add(request.user)
        from accounts.views import audit
        audit(request.user,'mail_read',kind='site_request',request=str(row.pk))
    return render(request,'postoffice/site_request.html',{'row':row,'values':[(LABELS.get(k,k),v) for k,v in row.data.items() if v]})


@staff
def attachment(request,pk):
    row=get_object_or_404(SiteRequest,pk=pk)
    if not row.resume_name:return HttpResponse(status=404)
    response=FileResponse(io.BytesIO(bytes(row.resume_data)),as_attachment=True,filename=row.resume_name,content_type='application/octet-stream')
    response['Cache-Control']='private, no-store'
    return response


@require_POST
@staff
def retry(request,pk):
    if not request.user.is_leader:raise PermissionDenied
    with transaction.atomic():
        row=get_object_or_404(SiteRequest.objects.select_for_update(),pk=pk)
        if row.delivery_state=='uncertain' and request.POST.get('checked')!='1':return HttpResponse('Сначала проверьте получение письма в info.',status=400)
        if row.delivery_state not in ('failed','uncertain'):return redirect('site_request',pk=pk)
        row.delivery_state='queued';row.delivery_error='';row.next_attempt_at=timezone.now();row.save(update_fields=['delivery_state','delivery_error','next_attempt_at','updated_at'])
        from accounts.views import audit
        audit(request.user,'mail_prepared',kind='site_request_retry',request=str(row.pk))
    return redirect('site_request',pk=pk)


@require_POST
@staff
def delete(request,pk):
    if not request.user.is_leader:raise PermissionDenied
    with transaction.atomic():
        row=get_object_or_404(SiteRequest.objects.select_for_update(),pk=pk)
        if row.delivery_state=='sending':return HttpResponse('Дождитесь завершения отправки письма.',status=409)
        row.delete()
    return redirect('site_requests')
