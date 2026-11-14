"""Short daily-photo flow. Report completion remains a separate conversation."""
import uuid
from datetime import timedelta
from django.core.exceptions import PermissionDenied,ValidationError
from django.db.models import Case,When,Value,IntegerField
from django.utils import timezone
from documents.storage import validate_upload,store_file
from repairs.progress import STAGES,ProgressForm
from repairs.services import report_work
from projects.access import contract_card
from .models import Draft,Photo
from .transport import download_photo


def available(user):
    from .dialog import available as assigned
    return assigned(user).exclude(section__project_object__status='archived')


def active(binding):
    return Draft.objects.filter(binding=binding,action='progress',state__in=['photos','editing']).order_by('-created_at').first()


def title(user,job):
    from .dialog import job_title
    contract=contract_card(job.contract,user)['title'] if job.contract_id else 'Без контракта'
    return 'Контракт: '+contract+'\n'+job_title(job)


def prompt(event,binding,draft):
    from .dialog import queue,button,report_date
    labels=[dict(STAGES)[key] for key in draft.raw.get('stages',[])]
    text=title(binding.user,draft.job)+'\n\nФото хода работ\nДата работ: '+report_date(draft.raw['performed_date']).strftime('%d.%m.%y')
    text+='\nЧто сделано: '+(', '.join(labels) if labels else 'не указано — необязательно')
    if draft.raw.get('note'):text+='\nПодпись: '+draft.raw['note'][:500]
    text+=f'\nФотографий: {draft.photos.count()}.\nНажмите скрепку и отправьте фотографии в этот чат. Подпись можно отправить обычным сообщением.\nДо 20 фото PNG/JPEG, до 10 МБ каждое и до 40 МБ за фотоотчёт.\nСохранение не завершает ремонт и не списывает материалы.'
    buttons=[button('Что сделано — необязательно',f'progress:stages:{draft.pk}'),button('Изменить дату',f'progress:date:{draft.pk}'),
        button('Сохранить фотоотчёт',f'progress:save:{draft.pk}'),button('Удалить добавленные фото',f'progress:clear:{draft.pk}'),button('Отменить фотоотчёт','/отмена')]
    queue(event,text,buttons,binding=binding,job=draft.job)


def handle(event,binding,stored_keys):
    from .dialog import queue,button,load_files,report_date
    text=event.payload.get('text','').strip();photos=event.payload.get('photos',[])
    draft=active(binding)
    requested=text in ('/фото','/ход_работ') or text.startswith('progress:')
    ordinary=bool(draft) and (photos or (bool(text) and not text.startswith('/') and not text.startswith(('progress:','review:','confirm:','edit:','answer:','default:','skip:','leader:','scope:','job:','section:','begin:','sections:'))))
    if not requested and not ordinary and not (draft and text=='/отмена'):return False
    user=binding.user
    if text in ('/фото','/ход_работ') and draft:
        if not available(user).filter(pk=draft.job_id).exists():raise PermissionDenied
        if draft.expires<=timezone.now():
            queue(event,'Срок черновика истёк. Чтобы продолжить, подтвердите выбранную работу:\n'+title(user,draft.job),[button('Продолжить фотоотчёт',f'progress:select:{draft.job_id}'),button('Отменить','/отмена')],binding=binding,job=draft.job);return True
        prompt(event,binding,draft);return True
    parts=text.split(':')
    action=parts[1] if text.startswith('progress:') and len(parts)>=3 else ''
    if text in ('/фото','/ход_работ') or action=='list':
        page=int(parts[2]) if action=='list' else 0
        if not 0<=page<=10000:raise ValidationError('Неверная страница.')
        if draft and draft.photos.exists():raise ValidationError('Сначала сохраните или отмените текущий фотоотчёт. Вернуться к нему: /фото')
        priority=Case(When(state__in=['progress','partial'],then=Value(0)),default=Value(1),output_field=IntegerField())
        jobs=list(available(user).order_by(priority,'section__project_object__name','section__name','pk')[page*6:page*6+7])
        buttons=[]
        for index,job in enumerate(jobs[:6],1):
            defects=job.active_defects;first=defects[0] if defects else None
            label=(f'км {first.km} · №{first.number}' if first else 'Ремонт')+' · '+job.section.name
            buttons.append(button(label,f'progress:select:{job.pk}'))
        if len(jobs)>6:buttons.append(button('Следующие работы',f'progress:list:{page+1}'))
        buttons.append(button('Меню','/menu'))
        queue(event,'Выберите работу для фотографий. Начатые работы показаны первыми.\n\n'+'\n\n'.join(title(user,j)[:450] for j in jobs[:6]) if jobs else 'Доступных работ для фотоотчёта нет.',buttons,binding=binding,scope={'jobs':[str(j.pk) for j in jobs[:6]]})
        return True
    if action=='select':
        job=available(user).filter(pk=uuid.UUID(parts[2])).first()
        if not job:raise PermissionDenied
        if draft:
            if draft.job_id==job.pk:
                draft.expires=timezone.now()+timedelta(hours=24);draft.save(update_fields=['expires']);prompt(event,binding,draft);return True
            if draft.photos.exists():raise ValidationError('Есть несохранённые фото. Вернитесь /фото и сохраните или отмените их.')
        other=Draft.objects.filter(binding=binding,state__in=['editing','photos','confirm']).exclude(action='progress').first()
        if other:raise ValidationError('Сначала завершите или отмените начатое действие командой /отмена.')
        Draft.objects.filter(binding=binding,action='progress',state__in=['editing','photos']).update(state='cancelled')
        draft=Draft.objects.create(binding=binding,job=job,action='progress',state='photos',fields=[],raw={
            'token':str(uuid.uuid4()),'expected_version':job.version,'performed_date':timezone.localdate().isoformat(),'stages':[],'note':''},expires=timezone.now()+timedelta(hours=24))
        prompt(event,binding,draft);return True
    if action:
        draft=Draft.objects.select_for_update().filter(pk=uuid.UUID(parts[2]),binding=binding,action='progress').select_related('job').first()
    if not draft:raise ValidationError('Выберите работу: /фото')
    from repairs.access import reportable_jobs
    if not reportable_jobs(user).filter(pk=draft.job_id).exists():raise PermissionDenied
    if draft.state=='submitted':
        queue(event,'Фотоотчёт уже сохранён. Повторной записи нет.',[button('Добавить ещё на этот км',f'progress:select:{draft.job_id}'),button('Выбрать другую работу','progress:list:0')],binding=binding,job=draft.job)
        return True
    if draft.state=='cancelled' or draft.expires<=timezone.now():raise ValidationError('Фотоотчёт истёк или отменён. Выберите работу: /фото')
    if text=='/отмена':
        draft.state='cancelled';draft.save(update_fields=['state']);queue(event,'Фотоотчёт отменён.',[button('Выбрать работу','/фото')],binding=binding);return True
    if action and action!='date' and '_input' in draft.raw:
        draft.raw.pop('_input',None);draft.save(update_fields=['raw'])
    if action=='refresh':
        if not available(user).filter(pk=draft.job_id).exists():raise ValidationError('Работа уже закрыта. Проверьте карточку в ЛК.')
        draft.raw['expected_version']=draft.job.version;draft.save(update_fields=['raw'])
        prompt(event,binding,draft);return True
    if action=='save' and draft.job.version!=int(draft.raw['expected_version']):
        queue(event,'Работа изменилась. Фотографии и подпись сохранены в черновике. Убедитесь, что они относятся к этой работе:\n'+title(user,draft.job),
              [button('Подтвердить работу и продолжить',f'progress:refresh:{draft.pk}'),button('Отменить фотоотчёт','/отмена')],binding=binding,job=draft.job);return True
    if action=='save':
        form=ProgressForm(draft.raw,load_files(draft),job=draft.job,user=user)
        if not form.is_valid():raise ValidationError([message for errors in form.errors.values() for message in errors])
        report=report_work(user,draft.job_id,'note',form.cleaned_data)
        stored_keys.extend(report.attachments.values_list('storage_key',flat=True))
        draft.report=report;draft.state='submitted';draft.save(update_fields=['report','state'])
        queue(event,'Фотоотчёт сохранён.\n'+title(user,draft.job)+'\nДата работ: '+report.performed_date.strftime('%d.%m.%y')+f'\nФотографий: {draft.photos.count()}. Ремонт остаётся в работе.',
              [button('Добавить ещё на этот км',f'progress:select:{draft.job_id}'),button('Выбрать другую работу','progress:list:0')],binding=binding,job=draft.job);return True
    if action=='date':
        draft.raw['_input']='date';draft.save(update_fields=['raw']);queue(event,'Введите дату работ в формате ДД.ММ.ГГ. По умолчанию используется сегодня.',[button('Оставить прежнюю дату',f'progress:back:{draft.pk}')],binding=binding,job=draft.job);return True
    if action=='stages' or action=='toggle':
        if action=='toggle':
            if len(parts)!=4 or parts[3] not in dict(STAGES):raise ValidationError('Неизвестный этап.')
            selected=set(draft.raw.get('stages',[]));selected.symmetric_difference_update([parts[3]])
            draft.raw['stages']=[key for key,label in STAGES if key in selected];draft.save(update_fields=['raw'])
        buttons=[button(('✓ ' if key in draft.raw.get('stages',[]) else '')+label,f'progress:toggle:{draft.pk}:{key}') for key,label in STAGES]
        buttons.append(button('Готово — вернуться к фото',f'progress:back:{draft.pk}'))
        queue(event,'Что сделано? Можно выбрать несколько действий или вернуться без выбора.',buttons,binding=binding,job=draft.job);return True
    if action=='clear':
        from django.db import transaction
        from documents.storage import private_path
        keys=list(draft.photos.values_list('storage_key',flat=True));draft.photos.all().delete()
        transaction.on_commit(lambda:[private_path(key).unlink(missing_ok=True) for key in keys])
    elif photos:
        if len(text)>3000:raise ValidationError('Подпись — до 3000 символов.')
        if draft.photos.count()+len(photos)>20:raise ValidationError('До 20 фото за одну отправку.')
        total=sum(draft.photos.values_list('size',flat=True))
        for photo in photos:
            if photo.get('type')!='image':raise ValidationError('Загрузите фотографии PNG/JPEG.')
            data=validate_upload(download_photo(photo['url']));total+=data['size']
            if total>40*1024*1024:raise ValidationError('До 40 МБ за один фотоотчёт.')
            key=store_file(data);stored_keys.append(key)
            Photo.objects.create(draft=draft,storage_key=key,**{k:data[k] for k in ('name','sha256','size')})
        if text and not text.startswith('progress:'):draft.raw['note']=text[:3000];draft.save(update_fields=['raw'])
        queue(event,title(user,draft.job)+f'\n\nДобавлено фотографий: {draft.photos.count()}. Можно отправить ещё или сохранить фотоотчёт.',
              [button('Сохранить фотоотчёт',f'progress:save:{draft.pk}'),button('Что сделано — необязательно',f'progress:stages:{draft.pk}'),button('Дата и подпись',f'progress:back:{draft.pk}')],binding=binding,job=draft.job);return True
    elif not action:
        if draft.raw.pop('_input','')=='date':
            value=report_date(text)
            if value>timezone.localdate():raise ValidationError('Дата не может быть в будущем.')
            draft.raw['performed_date']=value.isoformat()
        else:
            if len(text)>3000:raise ValidationError('Подпись — до 3000 символов.')
            draft.raw['note']=text
        draft.save(update_fields=['raw'])
    elif action!='back':raise ValidationError('Неизвестное действие фотоотчёта.')
    prompt(event,binding,draft);return True
