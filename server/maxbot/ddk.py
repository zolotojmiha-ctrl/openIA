"""Native MAX DDK revision. Drafts are durable; only confirmation changes the job."""
import uuid,json,hashlib
from datetime import timedelta
from django import forms
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.db.models import Min,Q
from django.utils import timezone
from documents.storage import validate_upload,store_file,private_path
from repairs import ddk as service
from repairs.construction import COUPLING_METHODS
from repairs.models import METHODS
from .models import Draft,Photo
from .transport import download_photo,download_file
from .labels import location

STATES=('editing','photos','confirm')


def active(binding):
    return Draft.objects.filter(binding=binding,action='ddk',state__in=STATES).order_by('-created_at').first()


def available(user):
    return service.allowed(user).filter(state__in=['planned','progress','partial']).exclude(contract__status='archived').exclude(section__project_object__status='archived').exclude(defects__archived_at__isnull=False).exclude(installed_couplings__isnull=False)


def fields(method):
    return ['method']+(['length_m']+(['composition'] if method!='p2' else [])+['wall_mm'] if method in COUPLING_METHODS else [])+['date','reason']


def form_for(draft,files=None):
    return service.DDKForm(draft.raw,files)


def title(job):
    defects=job.active_defects
    rows=[f'км {d.km} · № {d.number}' for d in defects[:3]]
    if len(defects)>3:rows.append(f'Ещё дефектов общей работы: {len(defects)-3}')
    return location(job.section.project_object.name,job.section.name,'\n')+'\n'+'\n'.join(rows)


def signature(draft):
    return hashlib.sha256(json.dumps([draft.raw,list(draft.photos.order_by('pk').values_list('name','sha256','size'))],sort_keys=True).encode()).hexdigest()


def delivery_allowed(binding,scope):
    try:draft=Draft.objects.select_related('job').filter(pk=scope.get('ddk_draft'),binding=binding,action='ddk').first()
    except (ValueError,ValidationError):return False
    if not draft or draft.state!=scope.get('ddk_state') or draft.step!=scope.get('ddk_step') or signature(draft)!=scope.get('ddk_signature'):return False
    if not service.allowed(binding.user).filter(pk=draft.job_id).exists():return False
    if draft.state=='submitted':return True
    if not available(binding.user).filter(pk=draft.job_id).exists():return False
    return bool(scope.get('ddk_refresh') or draft.expires>timezone.now() and draft.job.version==int(draft.raw['expected_version']))


def emit(event,binding,draft,text,buttons=(),*,refresh=False):
    from .dialog import queue
    queue(event,text,buttons,binding=binding,job=draft.job,scope={'ddk_draft':str(draft.pk),'ddk_state':draft.state,'ddk_step':draft.step,'ddk_signature':signature(draft),'ddk_refresh':refresh})


def prompt(event,binding,draft):
    from .dialog import button
    if draft.step>=len(draft.fields):
        draft.state='photos';draft.save(update_fields=['state'])
        emit(event,binding,draft,title(draft.job)+'\n\nПрикрепите заключение ДДК или фотографии скрепкой в этом чате.\nДо 20 файлов, до 10 МБ каждый и до 40 МБ всего.\n'
             f'Добавлено вложений: {draft.photos.count()}.\nКогда закончите или если вложений нет, нажмите «Проверить изменение».',
             [button('Проверить изменение',f'ddk:review:{draft.pk}'),button('Исправить ответы',f'ddk:edit:{draft.pk}'),button('Удалить вложения',f'ddk:clear:{draft.pk}'),button('Отменить','/отмена')])
        return
    name=draft.fields[draft.step];field=form_for(draft).fields[name]
    nonce=uuid.uuid4().hex
    draft.raw['_ddk_question']=nonce;draft.save(update_fields=['raw'])
    if name=='method':buttons=[button(label,f'ddk:answer:{draft.pk}:{draft.step}:{nonce}:{key}') for key,label in METHODS]
    else:
        value=draft.raw.get(name);buttons=[]
        if value:
            label=forms.DateField().clean(value).strftime('%d.%m.%Y') if name=='date' else value
            buttons.append(button('Оставить основание' if name=='reason' else 'Оставить: '+str(label),f'ddk:default:{draft.pk}:{draft.step}:{nonce}'))
        if name=='composition':buttons.append(button('Одна цельная муфта',f'ddk:skip:{draft.pk}:{draft.step}:{nonce}'))
    help_text={'length_m':'Введите общую длину в метрах, например 3 или 1,5.',
               'composition':'Для сборной конструкции введите длины через плюс, например 1,5+1,5. Для цельной нажмите кнопку ниже.',
               'wall_mm':'Введите толщину стенки муфты в миллиметрах, например 10.',
               'date':'Введите дату ДДК: ДД.ММ.ГГГГ. Можно оставить показанную дату.',
               'reason':'Кратко укажите результат ДДК и основание смены метода. До 1000 символов. Вложения добавляются следующим шагом.'}.get(name,'Выберите новый метод ремонта.')
    buttons.append(button('Отменить','/отмена'))
    emit(event,binding,draft,title(draft.job)+f'\n\nИзменение после ДДК · {draft.step+1}/{len(draft.fields)}\n{field.label}\n'+help_text+('\nРанее введено: '+str(draft.raw[name]) if name=='reason' and draft.raw.get(name) else ''),buttons)


def validate(draft):
    from .dialog import load_files
    form=form_for(draft,load_files(draft))
    if not form.is_valid():
        errors=[]
        for name,messages in form.errors.items():
            label=form.fields[name].label if name in form.fields else 'Изменение ДДК'
            errors.extend(f'{label}: {message}' for message in messages)
        raise ValidationError(errors)
    return form


def review(event,binding,draft):
    from .dialog import button
    form=validate(draft);data=form.cleaned_data
    lines=[title(draft.job),'','Проверьте изменение после ДДК:',f'Было: {draft.job.get_method_display()}',f'Новый метод: {dict(METHODS)[data["method"]]}']
    if data['method'] in COUPLING_METHODS:
        lines += [f'Общая длина: {data["length_m"]} м',f'Состав: {data["composition"] or "одна цельная муфта"}',f'Стенка: {data["wall_mm"]} мм']
    lines += ['Дата ДДК: '+data['date'].strftime('%d.%m.%Y'),'Основание: '+data['reason'],f'Вложений: {draft.photos.count()}',
              'Изменится задание и потребность. Ремонт не будет отмечен завершённым; материалы не списываются. Несовместимое закрепление муфт будет снято.']
    text='\n'.join(lines)
    if len(text)>3900:raise ValidationError('Сводка слишком длинная. Сократите основание кнопкой «Исправить ответы».')
    draft.state='confirm';draft.save(update_fields=['state'])
    emit(event,binding,draft,text,[button('Сохранить изменение',f'ddk:save:{draft.pk}:{signature(draft)}'),button('Исправить ответы',f'ddk:edit:{draft.pk}'),button('Добавить вложения',f'ddk:files:{draft.pk}'),button('Отменить','/отмена')])


def answer(draft,value):
    name=draft.fields[draft.step];field=form_for(draft).fields[name]
    if name=='date':
        value=forms.DateField(input_formats=['%d.%m.%Y','%d.%m.%y','%Y-%m-%d']).clean(value)
        if value>timezone.localdate():raise ValidationError('Дата ДДК не может быть в будущем.')
        value=value.isoformat()
    elif isinstance(field,forms.DecimalField):value=str(field.clean(str(value).replace(',','.')))
    else:value=field.clean(value)
    if name=='method':
        if value!=draft.raw.get('method'):
            for key in ('length_m','composition','wall_mm'):draft.raw.pop(key,None)
        draft.fields=fields(value)
        if value=='p2':draft.raw['composition']=''
    draft.raw[name]=value;draft.step+=1
    draft.save(update_fields=['raw','step','fields'])


def start(event,binding,job):
    existing=active(binding)
    if existing:
        if existing.job_id==job.pk:return existing
        raise ValidationError('Есть несохранённое изменение ДДК. Вернитесь /ддк или отмените его командой /отмена.')
    if Draft.objects.filter(binding=binding,state__in=STATES).exists():
        raise ValidationError('Сначала завершите или отмените начатое действие. Отмена: /отмена.')
    return Draft.objects.create(binding=binding,job=job,action='ddk',fields=fields(''),raw={
        'token':str(uuid.uuid4()),'expected_version':job.version,'date':timezone.localdate().isoformat()},expires=timezone.now()+timedelta(hours=2))


def attach(draft,attachments,stored_keys):
    if draft.photos.count()+len(attachments)>20:raise ValidationError('Не более 20 вложений за одно изменение ДДК.')
    total=sum(draft.photos.values_list('size',flat=True))
    for item in attachments:
        if item.get('type')=='image':upload=download_photo(item.get('url'))
        elif item.get('type')=='file':upload=download_file(item.get('url'),item.get('name'))
        else:raise ValidationError('Отправьте фотографию или документ скрепкой, без голосового/видеосообщения.')
        data=validate_upload(upload);total+=data['size']
        if total>40*1024*1024:raise ValidationError('Общий размер вложений — до 40 МБ.')
        key=store_file(data);stored_keys.append(key)
        Photo.objects.create(draft=draft,storage_key=key,**{k:data[k] for k in ('name','sha256','size')})


def handle(event,binding,stored_keys):
    from .dialog import queue,button
    text=event.payload.get('text','').strip();attachments=event.payload.get('photos',[])
    current=active(binding)
    requested=text in ('/ддк','/ddk') or text.startswith('ddk:')
    ordinary=current and (attachments or bool(text) and not text.startswith('/') and not text.startswith(('progress:','review:','confirm:','edit:','answer:','default:','skip:','leader:','scope:','job:','section:','begin:','sections:')))
    if not requested and not ordinary and not (current and text=='/отмена'):return False
    user=binding.user
    if not user.is_active or not (user.role=='foreman' or service.can_prepare(user)):raise PermissionDenied
    if text=='/отмена':
        current.state='cancelled';current.save(update_fields=['state'])
        queue(event,'Изменение ДДК отменено. Задание не изменено.',[button('Мои работы','/работы')],binding=binding);return True
    parts=text.split(':');action=parts[1] if text.startswith('ddk:') and len(parts)>1 else ''
    if text in ('/ддк','/ddk') and current:action='resume';parts=['ddk','resume',str(current.pk)]
    if text in ('/ддк','/ddk') and not current or action=='list':
        page=int(parts[2]) if action=='list' else 0
        if not 0<=page<=10000:raise ValidationError('Неверная страница списка.')
        jobs=list(available(user).annotate(first_km=Min('defects__kilometre',filter=Q(defects__archived_at__isnull=True))).order_by('section__project_object__name','section__name','first_km','pk')[page*6:page*6+7])
        buttons=[]
        for job in jobs[:6]:
            defects=job.active_defects
            label=(f'км {defects[0].km} · № {defects[0].number}' if defects else 'Ремонт')+' · '+job.section.project_object.name
            buttons.append(button(label,f'ddk:select:{job.pk}'))
        if len(jobs)>6:buttons.append(button('Следующие работы',f'ddk:list:{page+1}'))
        if current:buttons.insert(0,button('Вернуться к изменению',f'ddk:resume:{current.pk}'))
        queue(event,'Выберите работу для изменения метода после ДДК.' if jobs else 'Доступных работ для изменения после ДДК нет.',buttons,binding=binding,scope={'jobs':[str(j.pk) for j in jobs[:6]]});return True
    if action=='select':
        job=available(user).filter(pk=uuid.UUID(parts[2])).first()
        if not job:raise PermissionDenied
        draft=start(event,binding,job)
    elif action:
        draft=Draft.objects.select_for_update(of=('self',)).select_related('job').filter(pk=uuid.UUID(parts[2]),binding=binding,action='ddk').first()
    else:draft=Draft.objects.select_for_update(of=('self',)).select_related('job').filter(pk=current.pk,binding=binding,action='ddk').first()
    if not draft:raise ValidationError('Черновик не найден. Выберите работу: /ддк.')
    if not service.allowed(user).filter(pk=draft.job_id).exists():raise PermissionDenied
    if draft.state=='submitted':
        emit(event,binding,draft,'Изменение ДДК уже сохранено. Повторной записи нет.');return True
    if draft.state=='cancelled':raise ValidationError('Черновик отменён. Выберите работу: /ддк.')
    if not available(user).filter(pk=draft.job_id).exists():raise ValidationError('Работа закрыта или уже внесён монтаж. Изменение метода недоступно.')
    if action=='refresh':
        draft.raw['expected_version']=draft.job.version;draft.expires=timezone.now()+timedelta(hours=2)
        draft.state='editing';draft.step=0;draft.save(update_fields=['raw','expires','state','step'])
        prompt(event,binding,draft);return True
    if draft.expires<=timezone.now() or draft.job.version!=int(draft.raw['expected_version']):
        emit(event,binding,draft,title(draft.job)+'\n\nЗадание изменилось или истёк срок черновика. Ответы и вложения сохранены. Пересмотрите их по актуальному заданию перед подтверждением.',
             [button('Пересмотреть ответы',f'ddk:refresh:{draft.pk}'),button('Отменить','/отмена')],refresh=True);return True
    if action in ('select','resume'):
        if draft.state=='confirm':review(event,binding,draft)
        else:prompt(event,binding,draft)
    elif action=='save':
        if draft.state!='confirm':raise ValidationError('Сначала проверьте сводку изменения.')
        if len(parts)!=4 or parts[3]!=signature(draft):raise ValidationError('Сводка изменилась. Нажмите «Сохранить изменение» под последней сводкой.')
        form=validate(draft);report=service.change(user,draft.job_id,form.cleaned_data)
        stored_keys.extend(report.attachments.values_list('storage_key',flat=True))
        draft.report=report;draft.state='submitted';draft.save(update_fields=['report','state'])
        emit(event,binding,draft,'Изменение метода после ДДК сохранено в платформе. Исходное задание, основание и вложения — в истории. Монтаж и фактическое покрытие внесите после выполнения ремонта.',
             [button('Мои работы','/работы')])
    elif action=='edit':
        draft.step=0;draft.state='editing';draft.save(update_fields=['step','state']);prompt(event,binding,draft)
    elif action=='files':
        if draft.step<len(draft.fields):raise ValidationError('Сначала заполните ответы.')
        prompt(event,binding,draft)
    elif action=='review':
        if draft.state!='photos':raise ValidationError('Сначала заполните ответы и добавьте вложения при необходимости.')
        review(event,binding,draft)
    elif action=='clear':
        if draft.state!='photos':raise ValidationError('Для изменения вложений вернитесь к их добавлению.')
        keys=list(draft.photos.values_list('storage_key',flat=True));draft.photos.all().delete()
        transaction.on_commit(lambda:[private_path(key).unlink(missing_ok=True) for key in keys]);prompt(event,binding,draft)
    elif attachments:
        if draft.state=='editing' and draft.fields[draft.step]=='reason' and text and not action:answer(draft,text)
        if draft.step<len(draft.fields) or draft.state=='confirm':raise ValidationError('Вложения добавляются после ответов. Для дополнения подтверждённой сводки нажмите «Добавить вложения».')
        if draft.state=='photos' and text and not action:
            draft.raw['reason']=form_for(draft).fields['reason'].clean(draft.raw.get('reason','')+'\n'+text)
            draft.save(update_fields=['raw'])
        attach(draft,attachments,stored_keys);prompt(event,binding,draft)
    elif draft.state=='editing':
        name=draft.fields[draft.step];value=text
        if action in ('answer','default','skip'):
            if len(parts)<5 or int(parts[3])!=draft.step or parts[4]!=draft.raw.get('_ddk_question'):raise ValidationError('Это кнопка предыдущего вопроса. Ответьте на текущий вопрос.')
            value=':'.join(parts[5:]) if action=='answer' else draft.raw.get(name,'') if action=='default' else ''
            if action=='skip' and name!='composition':raise ValidationError('Этот ответ нельзя пропустить.')
        elif action:raise ValidationError('Используйте кнопки текущего вопроса.')
        answer(draft,value);prompt(event,binding,draft)
    else:prompt(event,binding,draft)
    return True
