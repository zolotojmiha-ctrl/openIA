from django import forms
from accounts.models import User
from documents.models import Person
from .models import Mailbox

class RequestForm(forms.Form):
    address=forms.EmailField(label='Адрес корпоративной почты')
    owner=forms.ModelChoiceField(label='Учётная запись сотрудника',queryset=User.objects.none(),required=False)
    person=forms.ModelChoiceField(label='Сотрудник из персонала',queryset=Person.objects.none(),required=False)
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.fields['owner'].queryset=User.objects.filter(is_active=True).exclude(role='customer').order_by('full_name');self.fields['owner'].label_from_instance=lambda u:u.full_name
        self.fields['person'].queryset=Person.objects.filter(legal_entity__tax_id='8604073687',archived=False).order_by('full_name')
class ConnectForm(RequestForm):
    version=forms.IntegerField(widget=forms.HiddenInput)
    password=forms.CharField(label='Пароль почтового ящика (не пароль ЛК)',required=False,max_length=256,widget=forms.PasswordInput)
    enabled=forms.BooleanField(label='Разрешить доступ к ящику внутри ЛК',required=False)
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.fields['address'].disabled=True
class ComposeForm(forms.Form):
    source=forms.CharField(widget=forms.HiddenInput,required=False)
    forward_attachments=forms.BooleanField(label='Переслать вложения исходного письма',required=False)
    def __init__(self,*args,forward_mode=False,**kwargs):
        super().__init__(*args,**kwargs)
        if not forward_mode:self.fields['forward_attachments'].widget=forms.HiddenInput()
    letter_id=forms.UUIDField(widget=forms.HiddenInput)
    version=forms.IntegerField(widget=forms.HiddenInput)
    to=forms.CharField(label='Кому',max_length=1500,required=False)
    cc=forms.CharField(label='Копия',max_length=1500,required=False)
    bcc=forms.CharField(label='Скрытая копия',max_length=1500,required=False)
    subject=forms.CharField(label='Тема',max_length=250,required=False)
    body=forms.CharField(label='Письмо',max_length=24000,required=False,help_text='До 24 000 символов; большие документы прикрепите файлом.',widget=forms.Textarea(attrs={'rows':12}))

def add_request_fields(form,actor):
    if actor and actor.is_leader:
        form.fields['request_mail']=forms.BooleanField(label='Нужна корпоративная почта (создать заявку руководителям)',required=False)
        form.fields['mail_address']=forms.EmailField(label='Адрес корпоративной почты',required=False,help_text='Например, ivanov@armada-sibir.ru. Ящик создаётся в Beget; статус заявки будет виден в ЛК.')

def validate_request(form,data):
    if 'request_mail' not in form.fields or not data.get('request_mail'):return data
    from django.conf import settings
    address=data.get('mail_address','').lower()
    if not address:form.add_error('mail_address','Укажите адрес почты.')
    elif address.rsplit('@',1)[-1]!=settings.ARMADA_MAIL_DOMAIN:form.add_error('mail_address','Используйте корпоративный домен.')
    elif Mailbox.objects.filter(address=address).exists():form.add_error('mail_address','Адрес уже добавлен. Откройте раздел «Почта».')
    data['mail_address']=address
    return data
