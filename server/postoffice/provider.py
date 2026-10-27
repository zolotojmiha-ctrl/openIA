"""Opt-in Beget mail provisioning. Fixed TLS endpoint; credentials never enter URLs/logs."""
import json,secrets
from urllib.request import Request,urlopen,HTTPRedirectHandler,build_opener,HTTPSHandler
from urllib.parse import urlencode
from datetime import timedelta
from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import render,redirect,get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters,sensitive_variables
from accounts.views import leader_required,audit
from .models import BegetSettings,MailboxProvision,Mailbox
from .crypto import seal,reveal

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValidationError('Beget вернул перенаправление; запрос остановлен.')

@sensitive_variables()
def api(config,method,data):
    if method not in ['getMailboxList','createMailbox']:raise ValidationError('Неизвестное действие API.')
    payload=urlencode({'login':config.login,'passwd':reveal(config.credential),'input_format':'json','output_format':'json','input_data':json.dumps(data)}).encode()
    request=Request('https://api.beget.com/api/mail/'+method,data=payload,method='POST',headers={'Content-Type':'application/x-www-form-urlencoded'})
    opener=build_opener(HTTPSHandler(),NoRedirect())
    try:
        with opener.open(request,timeout=15) as response:
            raw=response.read(512*1024+1)
        if len(raw)>512*1024:raise ValueError()
        result=json.loads(raw)
        if result.get('status')!='success':raise ValidationError('Beget отклонил запрос. Проверьте API-доступ и реквизиты в панели провайдера.')
        answer=result.get('answer')
        if not isinstance(answer,dict) or answer.get('status')!='success':raise ValidationError('Почтовая операция отклонена Beget. Проверьте домен, ограничения тарифа и состояние ящика в панели провайдера.')
        return answer.get('result')
    except ValidationError:raise
    except Exception:raise ValidationError('Нет подтверждённого ответа Beget. Проверьте подключение и результат в панели провайдера.') from None

def mailbox_names(config):
    result=api(config,'getMailboxList',{'domain':settings.ARMADA_MAIL_DOMAIN})
    if not isinstance(result,list):raise ValidationError('Не удалось прочитать список ящиков Beget. Автоматическое создание остановлено.')
    names=set()
    for row in result:
        value=row.get('mailbox') if isinstance(row,dict) else row
        if not isinstance(value,str):raise ValidationError('Неизвестный формат списка ящиков Beget. Создание остановлено.')
        names.add(value.split('@',1)[0].lower())
    return names

def queue(box):
    config=BegetSettings.objects.filter(pk=1,enabled=True).first()
    if not config:return False
    MailboxProvision.objects.get_or_create(mailbox=box,defaults={'credential':seal(secrets.token_urlsafe(24)),'mailbox_version':box.version})
    return True

@sensitive_variables()
def process_one():
    MailboxProvision.objects.filter(state='creating',updated_at__lt=timezone.now()-timedelta(minutes=10)).update(state='uncertain',error='Обработчик был прерван. Проверьте наличие ящика в Beget; повторное создание автоматически не выполняется.')
    config=BegetSettings.objects.filter(pk=1,enabled=True).first()
    if not config:return False
    with transaction.atomic():
        row=MailboxProvision.objects.select_for_update(skip_locked=True).filter(state='queued').select_related('mailbox').first()
        if not row:return False
        box=Mailbox.objects.select_for_update().get(pk=row.mailbox_id)
        if box.state!='requested' or box.version!=row.mailbox_version:
            row.state='failed';row.error='Настройки ящика изменились. Проверьте подключение вручную.';row.save();return True
        row.state='creating';row.save()
    phase='list';error='';state='failed'
    try:
        local,domain=box.address.rsplit('@',1)
        if domain!=settings.ARMADA_MAIL_DOMAIN:raise ValidationError('Почтовый домен не совпадает с корпоративным.')
        if local in mailbox_names(config):raise ValidationError('Ящик уже существует в Beget. Его пароль не изменялся. Подключите существующий ящик вручную.')
        phase='create'
        api(config,'createMailbox',{'domain':domain,'mailbox':local,'mailbox_password':reveal(row.credential)})
        state='active'
    except ValidationError as e:
        error='; '.join(e.messages)
        state='uncertain' if phase=='create' else 'failed'
    with transaction.atomic():
        box=Mailbox.objects.select_for_update().get(pk=row.mailbox_id)
        row=MailboxProvision.objects.select_for_update().get(pk=row.pk)
        if state=='active' and box.version==row.mailbox_version and box.state=='requested':
            box.credential=row.credential;box.state='active';box.version+=1;box.save()
        elif state=='active':state='uncertain';error='Ящик создан, но настройки в ЛК изменились. Проверьте подключение.'
        row.state=state;row.error=error;row.save()
        audit(None,'mailbox_updated',kind='provider_creation',mailbox=str(box.pk),state=state)
    return True

class SettingsForm(forms.Form):
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    login=forms.CharField(label='Логин API Beget',max_length=100)
    password=forms.CharField(label='Пароль API Beget (пусто — сохранить прежний)',required=False,widget=forms.PasswordInput)
    enabled=forms.BooleanField(label='Автоматически создавать новые корпоративные ящики в Beget',required=False)

@leader_required
@sensitive_post_parameters()
@sensitive_variables()
def setup(request):
    config,_=BegetSettings.objects.get_or_create(pk=1)
    form=SettingsForm(request.POST or None,initial={'expected_version':config.version,'login':config.login,'enabled':config.enabled})
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            config=BegetSettings.objects.select_for_update().get(pk=1)
            if config.version!=form.cleaned_data['expected_version']:form.add_error(None,'Настройки изменились. Обновите страницу.')
            elif not form.cleaned_data['password'] and not config.credential:form.add_error('password','Укажите пароль API.')
            else:
                config.login=form.cleaned_data['login'];config.enabled=form.cleaned_data['enabled']
                if form.cleaned_data['password']:config.credential=seal(form.cleaned_data['password'])
                config.version+=1;config.save();audit(request.user,'mailbox_updated',kind='provider_settings',enabled=config.enabled)
                messages.success(request,'Настройки API сохранены. Для проверки используйте отдельную кнопку.');return redirect('beget_setup')
    return render(request,'postoffice/provider.html',{'form':form,'configured':bool(config.credential),'config':config})

@leader_required
def check(request):
    if request.method=='POST':
        config=get_object_or_404(BegetSettings,pk=1)
        try:names=mailbox_names(config)
        except ValidationError as e:messages.error(request,'; '.join(e.messages))
        else:messages.success(request,f'API доступен. Для домена найдено ящиков: {len(names)}. Никаких ящиков не создавалось.')
    return redirect('beget_setup')

@leader_required
def provision(request,pk):
    box=get_object_or_404(Mailbox,pk=pk)
    if request.method=='POST':
        with transaction.atomic():
            box=Mailbox.objects.select_for_update().get(pk=pk)
            if box.state!='requested':messages.error(request,'Ящик уже подключён или отключён. Создание не требуется.')
            elif not queue(box):messages.error(request,'Сначала подключите API Beget.')
            else:messages.success(request,'Запрос на создание сохранён. Состояние появится на этой странице; повторные запросы не создают дубликат.')
    return redirect('mailbox_configure',pk=pk)

@leader_required
def verify_created(request,pk):
    box=get_object_or_404(Mailbox,pk=pk)
    row=get_object_or_404(MailboxProvision,mailbox=box)
    if request.method=='POST' and row.state in ['uncertain','failed']:
        from types import SimpleNamespace
        from . import transport
        try:
            with transport.connect(SimpleNamespace(address=box.address,credential=row.credential)):pass
        except ValidationError:messages.error(request,'Не удалось войти с сохранёнными реквизитами создаваемого ящика. Проверьте результат в Beget; существующий пароль не изменялся.')
        else:
            with transaction.atomic():
                box=Mailbox.objects.select_for_update().get(pk=pk)
                if box.state!='requested' or box.version!=row.mailbox_version:messages.error(request,'Настройки уже изменены. Автоматическое подключение отменено.')
                else:
                    box.credential=row.credential;box.state='active';box.version+=1;box.save();row.state='active';row.error='';row.save()
                    audit(request.user,'mailbox_updated',kind='provider_verified',mailbox=str(pk));messages.success(request,'Ящик проверен и подключён. Письма не отправлялись.')
    return redirect('mailbox_configure',pk=pk)

@leader_required
def retry(request,pk):
    if request.method=='POST':
        with transaction.atomic():
            box=get_object_or_404(Mailbox.objects.select_for_update(),pk=pk)
            row=get_object_or_404(MailboxProvision.objects.select_for_update(),mailbox=box)
            if row.state!='failed' or box.state!='requested' or row.mailbox_version!=box.version:
                messages.error(request,'Повтор допускается только после отказа до создания. Неопределённый результат сначала проверьте в Beget.')
            elif not BegetSettings.objects.filter(pk=1,enabled=True).exists():messages.error(request,'Автоматическое создание выключено.')
            else:row.state='queued';row.error='';row.save();messages.success(request,'Создание снова поставлено в очередь.')
    return redirect('mailbox_configure',pk=pk)
