"""Native photo selection, edit input, preview, confirmation and cancellation."""
from datetime import timedelta
from django.core.exceptions import PermissionDenied,ValidationError
from django.utils import timezone
from repairs import photos as service
from repairs.access import visible_jobs
from repairs.models import Attachment
from .models import Draft


def delivery_allowed(binding,scope):
    if not visible_jobs(binding.user).filter(pk=scope['job']).exists():return False
    if scope.get('photo') and not service.accessible(binding.user).filter(pk=scope['photo'],revision=scope['revision']).exists():return False
    action=scope.get('action','')
    return service.can_delete(binding.user) if action=='delete' else service.can_edit(binding.user) if action=='edit' else True


def emit(event,binding,photo_or_job,text,buttons,action='',media=False):
    from .dialog import queue
    job=photo_or_job.report.job if isinstance(photo_or_job,Attachment) else photo_or_job
    scope={'job':str(job.pk),'action':action}
    if media:scope.update(photo=str(photo_or_job.pk),revision=photo_or_job.revision)
    from .progress import title
    queue(event,title(binding.user,job)+'\n\n'+text,buttons,binding=binding,job=job,scope={'photo_manager':scope},photo=photo_or_job if media else None)


def _handle(event,binding,stored_keys):
    from .dialog import button
    user=binding.user;text=event.payload.get('text','').strip();uploads=event.payload.get('photos',[])
    draft=Draft.objects.filter(binding=binding,action='photoedit',state='editing').order_by('-created_at').first()
    if text=='/отмена' and draft:
        draft.state='cancelled';draft.save(update_fields=['state']);emit(event,binding,draft.job,'Изменение фотографии отменено.',[button('Фотографии работы',f'photos:list:{draft.job_id}:0')]);return True
    if not text.startswith('photos:'):
        if not draft or (text.startswith('/') and not uploads):return False
        if draft.expires<=timezone.now():raise ValidationError('Черновик истёк. Выберите фотографию заново.')
        photo=service.accessible(user).get(pk=draft.raw['photo']);service._authorize(user,photo,'edit')
        if photo.revision!=draft.raw['revision']:raise ValidationError('Фотографию уже изменили. Откройте новый предпросмотр.')
        name=draft.raw['name'];caption=draft.raw['caption'];upload=None
        field=draft.raw['field']
        if field=='file':
            if len(uploads)!=1:raise ValidationError('Отправьте одну фотографию PNG/JPEG или /отмена.')
            if uploads[0].get('type')!='image':raise ValidationError('Нужна фотография PNG/JPEG.')
            from .transport import download_photo
            upload=download_photo(uploads[0]['url'])
        elif field=='name':name=text
        elif field=='caption':caption='' if text=='-' else text
        else:raise ValidationError('Неизвестное поле.')
        item=service.preview(user,photo.pk,'edit',name=name,caption=caption,upload=upload)
        if item.storage_key:stored_keys.append(item.storage_key)
        draft.state='submitted';draft.save(update_fields=['state'])
        show_preview(event,binding,item);return True
    parts=text.split(':');action=parts[1]
    if action=='list':
        job=visible_jobs(user).get(pk=parts[2]);page=int(parts[3])
        if not 0<=page<=10000:raise ValidationError('Неверная страница.')
        rows=list(service.accessible(user).filter(report__job=job).order_by('report__performed_date','pk')[page*8:page*8+9])
        buttons=[button(f'{row.report.performed_date:%d.%m.%y} · {row.name}',f'photos:select:{row.pk}') for row in rows[:8]]
        if page:buttons.append(button('Предыдущие фото',f'photos:list:{job.pk}:{page-1}'))
        if len(rows)>8:buttons.append(button('Следующие фото',f'photos:list:{job.pk}:{page+1}'))
        emit(event,binding,job,'Фотографии работы. Выберите конкретную фотографию.'+('' if rows else '\nФотографий нет.'),buttons);return True
    if action in ('confirm','cancel'):
        from repairs.models import PhotoOperation
        item=PhotoOperation.objects.get(pk=parts[2],actor=user)
        if action=='confirm':service.confirm(user,item.pk);item.photo.refresh_from_db()
        else:service.cancel(user,item.pk)
        emit(event,binding,item.photo,('Изменение фотографии сохранено: '+item.photo.name) if action=='confirm' else 'Изменение отменено.',[button('Фотографии работы',f'photos:list:{item.photo.report.job_id}:0')]);return True
    photo=service.accessible(user).get(pk=parts[2])
    if action=='select':
        buttons=[]
        if service.can_edit(user):buttons.extend([button('Изменить название',f'photos:name:{photo.pk}'),button('Изменить подпись',f'photos:caption:{photo.pk}'),button('Заменить файл',f'photos:file:{photo.pk}')])
        if service.can_delete(user):buttons.append(button('Удалить фотографию',f'photos:delete:{photo.pk}'))
        buttons.append(button('Другие фотографии',f'photos:list:{photo.report.job_id}:0'))
        emit(event,binding,photo,f'{photo.name}\n{photo.caption}\nДата: {photo.report.performed_date:%d.%m.%Y}\nВерсия: {photo.revision}\nРазмер: {photo.size} байт',buttons,media=True);return True
    if action=='delete':
        show_preview(event,binding,service.preview(user,photo.pk,'delete'));return True
    if action in ('name','caption','file'):
        service._authorize(user,photo,'edit')
        Draft.objects.filter(binding=binding,action='photoedit',state='editing').update(state='cancelled')
        Draft.objects.create(binding=binding,job=photo.report.job,action='photoedit',state='editing',raw={'photo':str(photo.pk),'revision':photo.revision,'name':photo.name,'caption':photo.caption,'field':action},expires=timezone.now()+timedelta(minutes=30))
        prompt={'name':'Отправьте новое название (до 200 символов).','caption':'Отправьте новую подпись (до 1000 символов), «-» — очистить.','file':'Прикрепите одну новую фотографию PNG/JPEG, до 10 МБ.'}[action]
        emit(event,binding,photo,photo.name+'\n'+prompt+'\nПосле ввода будет предпросмотр; изменений пока нет.',[button('Отменить','/отмена')],action='edit');return True
    raise ValidationError('Неизвестное действие фотографии.')


def show_preview(event,binding,item):
    from .dialog import button
    if item.action=='delete':
        text='Удалить 1 фотографию и её версии?\n'+item.photo.name+f'\nПривязок этого снимка в работе: {item.payload['report_links']}. Они также потеряют изображение.\nФайлы без других ссылок будут удалены необратимо. Общие файлы сохранятся; внешний архив будет очищен фоновым обработчиком после проверки ссылок. Аудит и контрольные суммы останутся.'
    else:
        text='Проверьте изменение фотографии:\nНазвание: '+item.payload['name']+'\nПодпись: '+item.payload['caption']+'\n'+('Файл будет заменён.' if item.storage_key else 'Файл не меняется.')+' Прежняя версия сохранится в истории. Изменение затронет все привязки этого снимка в этой работе: '+str(item.payload['report_links'])+'.'
    emit(event,binding,item.photo,text,[button('Подтвердить',f'photos:confirm:{item.pk}'),button('Отменить',f'photos:cancel:{item.pk}')],action=item.action)


def handle(event,binding,stored_keys):
    from django.core.exceptions import ObjectDoesNotExist
    try:return _handle(event,binding,stored_keys)
    except ObjectDoesNotExist:raise PermissionDenied from None
