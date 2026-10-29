"""Durable site receipts use the connected info mailbox, never a public relay."""
import smtplib
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formatdate
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from .models import SiteRequest, Mailbox
from .site_forms import LABELS, RECIPIENT
from . import transport


def message_for(row,box):
    msg=EmailMessage()
    msg['From']=box.address;msg['To']=RECIPIENT
    msg['Subject']='[Сайт] '+row.get_kind_display()+' · '+row.id.hex[:12]
    msg['Message-ID']=f'<site-{row.pk}@armada-sibir.ru>'
    msg['Date']=formatdate(localtime=False)
    if row.data.get('email'):msg['Reply-To']=row.data['email']
    body=[row.get_kind_display(),f'Номер обращения: {row.id.hex[:12]}',
          'Получено: '+timezone.localtime(row.created_at).strftime('%d.%m.%Y %H:%M'), '']
    body += [f'{LABELS.get(k,k)}: {v}' for k,v in row.data.items() if v]
    body += ['',f'Согласие на обработку данных получено при отправке. Редакция: {row.consent_version}.']
    msg.set_content('\n'.join(body))
    if row.resume_name:
        maintype,subtype=row.resume_type.split('/',1)
        msg.add_attachment(bytes(row.resume_data),maintype=maintype,subtype=subtype,filename=row.resume_name)
    return msg


def recover_interrupted():
    SiteRequest.objects.filter(delivery_state='sending',updated_at__lt=timezone.now()-timedelta(minutes=10)).update(
        delivery_state='uncertain',delivery_error='Отправка была прервана. Проверьте получение в info перед повтором.')


def cleanup_expired():
    """The public policy limits the site's own copies to 180 days."""
    from .models import SiteRequestAttempt
    SiteRequest.objects.filter(created_at__lt=timezone.now()-timedelta(days=180)).exclude(delivery_state='sending').delete()
    SiteRequestAttempt.objects.filter(window_start__lt=timezone.now()-timedelta(days=2)).delete()


def process_one():
    with transaction.atomic():
        row=SiteRequest.objects.select_for_update(skip_locked=True).filter(delivery_state='queued',next_attempt_at__lte=timezone.now()).order_by('created_at').first()
        if not row:return False
        box=Mailbox.objects.filter(address__iexact=RECIPIENT,state='active').exclude(credential='').first()
        if not box:
            row.delivery_error='Подключите почтовый ящик info@armada-sibir.ru. Обращение сохранено в кабинете.'
            row.next_attempt_at=timezone.now()+timedelta(minutes=5)
            row.save(update_fields=['delivery_error','next_attempt_at','updated_at'])
            return True
        row.delivery_state='sending';row.delivery_attempts+=1;row.delivery_error=''
        row.save(update_fields=['delivery_state','delivery_attempts','delivery_error','updated_at'])
    state='sent';error=''
    try:
        message=message_for(row,box)
        refused=transport.deliver(box,message,[RECIPIENT])
        if refused:state='failed';error='Почтовый сервер не принял получателя info. Обращение сохранено в кабинете.'
    except (smtplib.SMTPAuthenticationError,smtplib.SMTPRecipientsRefused,smtplib.SMTPSenderRefused,smtplib.SMTPDataError,ValidationError):
        state='failed';error='Почтовый сервер не принял письмо. Проверьте подключение ящика info.'
    except (OSError,smtplib.SMTPException):
        state='uncertain';error='Соединение оборвалось. Проверьте получение письма в info перед повторной отправкой.'
    with transaction.atomic():
        current=SiteRequest.objects.select_for_update().get(pk=row.pk)
        current.delivery_state=state;current.delivery_error=error;current.sent_at=timezone.now() if state=='sent' else None
        current.save(update_fields=['delivery_state','delivery_error','sent_at','updated_at'])
    return True
