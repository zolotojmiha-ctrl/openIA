"""Private invoice/act attachments: upload, explicit confirmation and cancellation."""
from datetime import timedelta
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.utils import timezone
from documents.storage import validate_upload,store_file,discard_file
from accounts.views import audit
from . import rentals
from .models import RentalInvoice,RentalInvoiceFile


def visible(user):
    if not rentals.financial_reader(user):return RentalInvoiceFile.objects.none()
    return RentalInvoiceFile.objects.filter(invoice__period__rental__in=rentals.visible(user),state='available')


def preview(user,pk,invoice_id,kind,upload,token,stored_keys=None):
    key=None
    try:
        with transaction.atomic():
            actor,rental=rentals.financial_lock(user,pk)
            invoice=RentalInvoice.objects.select_for_update().select_related('period').get(pk=invoice_id,period__rental=rental)
            if invoice.void_reason or invoice.period.reopened_at:raise ValidationError('Сверка отменена. Выберите действующую сверку.')
            if kind not in ('invoice','act','other'):raise ValidationError('Выберите вид документа.')
            data=validate_upload(upload)
            old=RentalInvoiceFile.objects.filter(token=token).first()
            if old:
                if (old.invoice_id,old.author_id,old.kind,old.sha256,old.size)!=(invoice.pk,actor.pk,kind,data['sha256'],data['size']):
                    raise ValidationError('Эта отправка уже использована для другого файла.')
                return old
            key=store_file(data)
            if stored_keys is not None:stored_keys.append(key)
            row=RentalInvoiceFile.objects.create(invoice=invoice,token=token,kind=kind,storage_key=key,name=data['name'],content_type=data['content_type'],sha256=data['sha256'],size=data['size'],author=actor,session_version=actor.session_version,expires=timezone.now()+timedelta(minutes=30))
            return row
    except Exception:
        if key:discard_file(key)
        raise


@transaction.atomic
def confirm(user,pk,file_id):
    actor,rental=rentals.financial_lock(user,pk)
    row=RentalInvoiceFile.objects.select_for_update().select_related('invoice__period').get(pk=file_id,invoice__period__rental=rental)
    if row.author_id!=actor.pk or row.session_version!=actor.session_version:raise PermissionDenied
    if row.state=='available':return row
    if row.state!='pending' or row.expires<=timezone.now():raise ValidationError('Подтверждение истекло или отменено. Загрузите файл заново.')
    if row.invoice.void_reason or row.invoice.period.reopened_at:raise ValidationError('Сверка отменена.')
    row.state='available';row.save(update_fields=['state'])
    audit(actor,'finance_created',kind='rental_invoice_file',file=str(row.pk),invoice=str(row.invoice_id),sha256=row.sha256)
    return row


@transaction.atomic
def cancel(user,pk,file_id):
    actor,rental=rentals.financial_lock(user,pk)
    row=RentalInvoiceFile.objects.select_for_update().get(pk=file_id,invoice__period__rental=rental,author=actor)
    if row.state=='available':raise ValidationError('Документ уже прикреплён. Отменять загрузку поздно.')
    key=row.storage_key;row.state='cancelled';row.storage_key='';row.save(update_fields=['state','storage_key'])
    if key:transaction.on_commit(lambda:discard_file(key))
    return row


def cleanup_expired(limit=100):
    """Bounded maintenance; manifests/audit survive, unconfirmed bytes expire."""
    count=0
    for pk in RentalInvoiceFile.objects.filter(state='pending',expires__lte=timezone.now()).values_list('pk',flat=True)[:limit]:
        with transaction.atomic():
            row=RentalInvoiceFile.objects.select_for_update().get(pk=pk)
            if row.state!='pending' or row.expires>timezone.now():continue
            key=row.storage_key;row.storage_key='';row.state='cancelled';row.save(update_fields=['storage_key','state'])
            if key:transaction.on_commit(lambda key=key:discard_file(key))
            count+=1
    return count
