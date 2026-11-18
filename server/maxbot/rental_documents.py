"""Invoice file selection and upload remain inside MAX; no private public URLs."""
import uuid
from datetime import datetime,timedelta
from django.core.exceptions import ObjectDoesNotExist,PermissionDenied,ValidationError
from django.utils import timezone
from projects import rental_files as service
from . import rental_finance as finance


def _handle(event,binding,stored_keys):
    from .dialog import button
    from .leadership import link
    text=event.payload.get('text','').strip()
    draft=binding.menu_scope.get('rental_file')
    if ':' in text and not text.startswith('rentfile:') and not event.payload.get('photos'):return False
    if not text.startswith('rentfile:') and (not draft or draft['state'] not in ('upload','confirm') or (text.startswith('/') and text!='/отмена')):return False
    parts=text.split(':')
    if text.startswith('rentfile:invoices:'):
        rental=finance.accessible(binding.user).get(pk=parts[2]);page=int(parts[3])
        if not 0<=page<=10000:raise ValidationError('Неверная страница.')
        rows=list(service.RentalInvoice.objects.filter(period__rental=rental).select_related('period').order_by('-pk')[page*5:page*5+6])
        buttons=[button(f'{row.reference} · {row.period.starts_on:%d.%m}–{row.period.ends_on:%d.%m.%Y}',f'rentfile:list:{rental.pk}:{row.pk}:0') for row in rows[:5]]
        if page:buttons.append(button('Предыдущие счета',f'rentfile:invoices:{rental.pk}:{page-1}'))
        if len(rows)>5:buttons.append(button('Следующие счета',f'rentfile:invoices:{rental.pk}:{page+1}'))
        finance.emit(event,binding,rental,'Выберите сверку для документов.' if rows else 'Сначала сохраните сверку счёта.',buttons);return True
    if text.startswith('rentfile:list:'):
        rental=finance.accessible(binding.user).get(pk=parts[2]);invoice=service.RentalInvoice.objects.get(pk=parts[3],period__rental=rental);page=int(parts[4])
        if not 0<=page<=10000:raise ValidationError('Неверная страница.')
        rows=list(service.visible(binding.user).filter(invoice=invoice)[page*5:page*5+6])
        buttons=[link(row.get_kind_display()+': '+row.name,'rental_invoice_file',[rental.pk,row.pk]) for row in rows[:5]]
        buttons += [link('Скачать расчёт Excel','rental_invoice_export',[rental.pk,invoice.pk]),link('Печать расчёта / PDF','rental_invoice_print',[rental.pk,invoice.pk])]
        if not invoice.void_reason and finance.rentals.financial_writer(binding.user):
            buttons += [button('Прикрепить '+label,f'rentfile:start:{rental.pk}:{invoice.pk}:{kind}') for kind,label in [('invoice','счёт'),('act','акт'),('other','другой документ')]]
        if page:buttons.append(button('Предыдущие файлы',f'rentfile:list:{rental.pk}:{invoice.pk}:{page-1}'))
        if len(rows)>5:buttons.append(button('Следующие файлы',f'rentfile:list:{rental.pk}:{invoice.pk}:{page+1}'))
        buttons.append(button('Другие счета',f'rentfile:invoices:{rental.pk}:0'))
        finance.emit(event,binding,rental,invoice.reference+'\nФайлы доступны только после проверки прав. Скачивание не подтверждает оплату.',buttons);return True
    if text.startswith('rentfile:start:'):
        rental=finance.accessible(binding.user).get(pk=parts[2]);invoice=service.RentalInvoice.objects.get(pk=parts[3],period__rental=rental)
        if not finance.rentals.financial_writer(binding.user):raise PermissionDenied
        if invoice.void_reason:raise ValidationError('Сверка отменена.')
        kind=parts[4]
        if kind not in ('invoice','act','other'):raise ValidationError('Неверный вид документа.')
        binding.menu_scope={**binding.menu_scope,'rental_file':{'state':'upload','rental':str(rental.pk),'invoice':invoice.pk,'kind':kind,'token':str(uuid.uuid4()),'expires':(timezone.now()+timedelta(minutes=30)).isoformat()}}
        binding.menu_scope.pop('rental_finance',None);binding.save(update_fields=['menu_scope'])
        finance.emit(event,binding,rental,'Прикрепите один файл как документ (до 10 МБ в MAX). Затем проверьте название и подтвердите загрузку.',[button('Отменить загрузку','rentfile:cancel')],write=True);return True
    if text.startswith('rentfile:confirm:') or text.startswith('rentfile:cancel:'):
        rental=finance.accessible(binding.user).get(pk=parts[2]);file_id=parts[3]
        if parts[1]=='confirm':row=service.confirm(binding.user,rental.pk,file_id)
        else:row=service.cancel(binding.user,rental.pk,file_id)
        if draft:
            draft['state']='done';binding.menu_scope={**binding.menu_scope,'rental_file':draft};binding.save(update_fields=['menu_scope'])
        finance.emit(event,binding,rental,'Файл прикреплён.' if parts[1]=='confirm' else 'Загрузка отменена.',[button('Документы счёта',f'rentfile:list:{rental.pk}:{row.invoice_id}:0')]);return True
    if not draft:raise ValidationError('Выберите счёт и загрузку заново.')
    rental=finance.accessible(binding.user).get(pk=draft['rental'])
    if text in ('/отмена','rentfile:cancel'):
        if draft.get('file'):service.cancel(binding.user,rental.pk,draft['file'])
        draft['state']='done';binding.menu_scope={**binding.menu_scope,'rental_file':draft};binding.save(update_fields=['menu_scope'])
        finance.emit(event,binding,rental,'Загрузка отменена.');return True
    if draft['state']!='upload':raise ValidationError('Файл ожидает подтверждения. Нажмите «Прикрепить» или «Отменить».')
    if timezone.now()>datetime.fromisoformat(draft['expires']):raise ValidationError('Время загрузки истекло. Выберите счёт заново.')
    uploads=event.payload.get('photos',[])
    if len(uploads)!=1 or uploads[0].get('type')!='file':raise ValidationError('Прикрепите один файл как документ или отмените загрузку.')
    from .transport import download_file
    upload=download_file(uploads[0].get('url'),uploads[0].get('name'))
    row=service.preview(binding.user,rental.pk,draft['invoice'],draft['kind'],upload,draft['token'],stored_keys)
    draft.update(state='confirm',file=str(row.pk));binding.menu_scope={**binding.menu_scope,'rental_file':draft};binding.save(update_fields=['menu_scope'])
    finance.emit(event,binding,rental,f'Проверьте: {row.get_kind_display()}\nСверка №{row.invoice_id}\n{row.name} · {row.size} байт\nФайл будет прикреплён после подтверждения.',[button('Прикрепить',f'rentfile:confirm:{rental.pk}:{row.pk}'),button('Отменить',f'rentfile:cancel:{rental.pk}:{row.pk}')],write=True)
    return True


def handle(event,binding,stored_keys):
    try:return _handle(event,binding,stored_keys)
    except ObjectDoesNotExist:raise PermissionDenied from None
    except (ValueError,IndexError,KeyError):raise ValidationError('Откройте документы аренды заново.') from None
