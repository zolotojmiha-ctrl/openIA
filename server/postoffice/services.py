import re,uuid,smtplib
from email.message import EmailMessage
from email.utils import getaddresses,formatdate
from django.conf import settings
from django.core.exceptions import ValidationError,PermissionDenied
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from accounts.models import User
from accounts.views import audit
from .models import Mailbox,Letter,Attachment
from .access import boxes,leader
from .crypto import seal
from . import transport

def addresses(value):
    if not value.strip():return []
    if any(c in value for c in '\r\n'):raise ValidationError('Адреса не должны содержать перенос строки.')
    parsed=getaddresses([value]);result=[]
    for _,address in parsed:
        validate_email(address);result.append(address)
    if len(result)>20:raise ValidationError('Не более 20 получателей в каждом поле.')
    if value.strip() and not result:raise ValidationError('Проверьте адрес получателя.')
    return result

def actor(user):
    fresh=User.objects.select_for_update().get(pk=user.pk)
    if not fresh.is_active or fresh.session_version!=user.session_version:raise PermissionDenied
    return fresh

def request_address(user,address,owner=None,person=None):
    address=address.strip().lower();validate_email(address)
    if person and owner and person.user_id!=owner.pk:raise ValidationError('Выберите сотрудника или его связанную учётную запись, а не разных людей.')
    if address.rsplit('@',1)[-1]!=settings.ARMADA_MAIL_DOMAIN:raise ValidationError('Используйте корпоративный почтовый домен.')
    with transaction.atomic():
        admin=actor(user);leader(admin)
        from projects.models import LegalEntity
        LegalEntity.objects.select_for_update().get(tax_id='8604073687')
        if owner:owner=User.objects.get(pk=owner.pk,is_active=True)
        if person and (person.archived or person.legal_entity.tax_id!='8604073687'):raise ValidationError('Сотрудник недоступен.')
        if Mailbox.objects.filter(address=address).exists():raise ValidationError('Такой адрес уже добавлен. Откройте существующий ящик.')
        box=Mailbox.objects.create(address=address,owner=owner,person=person,created_by=admin)
        audit(admin,'mailbox_updated',kind='requested',mailbox=str(box.pk),address=box.address)
        return box

def configure(user,box_id,version,password,owner,person,enabled):
    with transaction.atomic():
        admin=actor(user);leader(admin)
        box=Mailbox.objects.select_for_update().get(pk=box_id)
        if box.version!=version:raise ValidationError('Ящик изменён. Обновите страницу.')
        if owner and not User.objects.filter(pk=owner.pk,is_active=True).exists():raise ValidationError('Учётная запись неактивна.')
        if person and (person.archived or person.legal_entity.tax_id!='8604073687'):raise ValidationError('Сотрудник недоступен.')
        if person and owner and person.user_id!=owner.pk:raise ValidationError('Сотрудник и учётная запись не связаны.')
        if enabled and not password and not box.credential:raise ValidationError('Введите пароль существующего почтового ящика.')
        if not password and box.owner_id==getattr(owner,'pk',None) and box.person_id==getattr(person,'pk',None) and box.state==('active' if enabled else 'disabled'):return box
        if password:box.credential=seal(password)
        box.owner=owner;box.person=person;box.state='active' if enabled else 'disabled';box.version+=1;box.save()
        audit(admin,'mailbox_updated',kind='configured',mailbox=str(box.pk),owner=str(owner.pk) if owner else '',person=str(person.pk) if person else '',state=box.state)
        return box

@transaction.atomic
def save_letter(user,box_id,letter_id,version,data,files,queue=False):
    fresh=actor(user);box=boxes(fresh).select_for_update(of=('self',)).get(pk=box_id)
    if box.state!='active':raise ValidationError('Почтовый ящик ещё не подключён.')
    letter=Letter.objects.select_for_update().filter(pk=letter_id).first()
    if letter:
        if letter.mailbox_id!=box.pk or letter.author_id!=fresh.pk:raise PermissionDenied
        if letter.version!=version or letter.state!='draft':raise ValidationError('Письмо уже изменено или отправлено. Откройте исходящие.')
    else:
        if version!=0:raise ValidationError('Черновик не найден.')
        if Letter.objects.filter(author=fresh,state__in=['draft','queued','sending']).count()>=50:raise ValidationError('Сначала обработайте черновики и исходящие: лимит 50 писем.')
        letter=Letter(pk=letter_id,mailbox=box,author=fresh)
    for key in ('to','cc','bcc','subject','body'):setattr(letter,key,data.get(key,''))
    reply_id=data.get('_reply_id','')
    if reply_id and re.fullmatch(r'<[^\r\n<>]{1,190}>',reply_id):letter.in_reply_to=reply_id
    recipients=addresses(letter.to)+addresses(letter.cc)+addresses(letter.bcc)
    if queue and not recipients:raise ValidationError('Укажите получателя.')
    if any(c in letter.subject for c in '\r\n'):raise ValidationError('Тема должна быть одной строкой.')
    total=sum(len(a.data) for a in letter.attachments.all()) if not letter._state.adding else 0
    if len(files)+(letter.attachments.count() if not letter._state.adding else 0)>10:raise ValidationError('Не более 10 вложений.')
    loaded=[]
    for file in files:
        if file.size>10*1024*1024:raise ValidationError('Одно вложение — до 10 МБ.')
        content=file.read(10*1024*1024+1);total+=len(content)
        if len(content)>10*1024*1024 or total>15*1024*1024:raise ValidationError('Общий размер вложений — до 15 МБ.')
        loaded.append((file.name.replace('\\','/').split('/')[-1].replace('\r','').replace('\n','')[:200],content))
    letter.version+=1;letter.state='queued' if queue else 'draft'
    letter.message_id=f'<{letter.pk}@{settings.ARMADA_MAIL_DOMAIN}>';letter.full_clean();letter.save()
    for name,content in loaded:Attachment.objects.create(letter=letter,name=name,data=content)
    audit(fresh,'mail_prepared',letter=str(letter.pk),mailbox=str(box.pk),state=letter.state)
    return letter

def message_for(letter):
    msg=EmailMessage();msg['From']=letter.mailbox.address
    for key in ('to','cc'):
        if getattr(letter,key):msg[key.title()]=getattr(letter,key)
    if letter.in_reply_to:msg['In-Reply-To']=letter.in_reply_to;msg['References']=letter.in_reply_to
    msg['Subject']=letter.subject;msg['Date']=formatdate(localtime=False);msg['Message-ID']=letter.message_id
    msg.set_content(letter.body)
    for file in letter.attachments.all():msg.add_attachment(bytes(file.data),maintype='application',subtype='octet-stream',filename=file.name)
    return msg

def process_one():
    # SMTP cannot participate in a DB transaction. Ambiguous sends require manual review.
    with transaction.atomic():
        row=Letter.objects.select_for_update(skip_locked=True).filter(state='queued').order_by('created_at').first()
        if not row:return False
        fresh=User.objects.get(pk=row.author_id)
        if not boxes(fresh).filter(pk=row.mailbox_id,state='active').exists():
            row.state='failed';row.error='Доступ автора или ящика изменён.';row.save();return True
        row.state='sending';row.error='';row.save()
    error='';state='sent'
    try:
        msg=message_for(row);refused=transport.deliver(row.mailbox,msg,addresses(row.to)+addresses(row.cc)+addresses(row.bcc))
        if refused:error='Часть получателей не принята сервером. Проверьте адресатов; повторная отправка не выполняется автоматически.'
    except (smtplib.SMTPAuthenticationError,smtplib.SMTPRecipientsRefused,smtplib.SMTPSenderRefused,smtplib.SMTPDataError,ValidationError):state='failed';error='Сервер не принял письмо. Проверьте реквизиты ящика и адресатов.'
    except (OSError,smtplib.SMTPException):state='uncertain';error='Соединение оборвалось. Перед повторной отправкой проверьте отправленные и получателя.'
    if state=='sent':
        try:transport.save_sent(row.mailbox,msg.as_bytes())
        except (ValidationError,OSError):error=(error+' ' if error else '')+'Письмо принято сервером, но копию в отправленных сохранить не удалось.'
    with transaction.atomic():
        row=Letter.objects.select_for_update().get(pk=row.pk);row.state=state;row.error=error;row.sent_at=timezone.now() if state=='sent' else None;row.save()
        audit(row.author,'mail_sent',letter=str(row.pk),mailbox=str(row.mailbox_id),state=state)
    return True
