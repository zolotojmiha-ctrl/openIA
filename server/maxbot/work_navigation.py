"""Native section-first pickers shared by work, DDK and daily-photo actions."""
import uuid
from django.core.exceptions import PermissionDenied,ValidationError
from django.db.models import Min,Q,F
from repairs.access import visible_jobs,visible_sections
from .labels import section_button_title

PURPOSES={'works':'работы и дефекты','ddk':'изменение метода после ДДК','photos':'добавление фото хода работ','history':'полученные фотоотчёты'}


def jobs_for(user,purpose,contract=''):
    if purpose not in PURPOSES:raise ValidationError('Неизвестный список работ.')
    if purpose=='ddk':
        from .ddk import available
        rows=available(user)
    elif purpose=='photos':
        from .progress import available
        rows=available(user)
    elif purpose=='history':rows=visible_jobs(user).filter(reports__context__progress__isnull=False).distinct()
    else:rows=visible_jobs(user).exclude(state__in=['confirmed','reported','unrepairable'])
    rows=rows.exclude(contract__status='archived').exclude(section__project_object__status='archived')
    if user.role in ('customer','lessor') and not user.is_leader:return rows.none()
    return rows.filter(contract_id=contract) if contract else rows


def emit(event,binding,text,buttons,purpose,**details):
    from .dialog import queue
    queue(event,text,buttons,binding=binding,scope={'work_navigation':{'purpose':purpose,'contract':binding.menu_scope.get('contract',''),**details}})


def sections(event,binding,purpose,page=0):
    from .dialog import button
    if not 0<=page<=10000:raise ValidationError('Неверная страница.')
    jobs=jobs_for(binding.user,purpose,binding.menu_scope.get('contract',''))
    rows=list(visible_sections(binding.user).filter(pk__in=jobs.values('section_id')).select_related('project_object').order_by('project_object__name','pipeline','pk')[page*6:page*6+7])
    buttons=[button(section_button_title(s),f'worknav:jobs:{purpose}:{s.pk}:0') for s in rows[:6]]
    if len(rows)>6:buttons.append(button('Далее',f'worknav:sections:{purpose}:{page+1}'))
    if page:buttons.append(button('Назад',f'worknav:sections:{purpose}:{page-1}'))
    if purpose=='ddk':
        from .ddk import active
        draft=active(binding)
        if draft:buttons.append(button('Вернуться к изменению ДДК',f'ddk:resume:{draft.pk}'))
    if purpose in ('photos','history'):buttons.append(button('Полученные фотоотчёты' if purpose=='photos' else 'Добавить фотографии','worknav:sections:history:0' if purpose=='photos' else '/фото'))
    buttons.append(button('Меню','/меню'))
    body=f'Выберите участок с доступными работами: {PURPOSES[purpose]}.' if rows else f'Нет доступных текущих работ: {PURPOSES[purpose]}.'
    if purpose=='history' and not rows:body='Полученных ежедневных фотоотчётов по доступным участкам пока нет. Можно добавить фотографии кнопкой ниже.'
    if binding.menu_scope.get('contract'):
        from projects.access import visible_contracts,contract_card
        selected=visible_contracts(binding.user).filter(pk=binding.menu_scope['contract']).first()
        if selected:body='Контракт: '+contract_card(selected,binding.user)['title']+'\n'+body
    emit(event,binding,body,buttons,purpose,sections=[str(s.pk) for s in rows[:6]])


def listing(event,binding,purpose,section_id,page=0):
    from .dialog import button,job_button_title
    if not 0<=page<=10000:raise ValidationError('Неверная страница.')
    section=visible_sections(binding.user).filter(pk=section_id).first()
    if not section:raise PermissionDenied
    contract=binding.menu_scope.get('contract','')
    if contract and not section.project_object.contracts.filter(pk=contract).exists():raise PermissionDenied
    selected=binding.menu_scope.copy();selected['section']=str(section.pk);binding.menu_scope=selected;binding.save(update_fields=['menu_scope'])
    query=jobs_for(binding.user,purpose,contract).filter(section=section)
    rows=list(query.annotate(first_km=Min('defects__kilometre',filter=Q(defects__archived_at__isnull=True))).order_by(F('first_km').asc(nulls_last=True),'pk')[page*6:page*6+7])
    def target(j):return f'ddk:select:{j.pk}' if purpose=='ddk' else f'progress:select:{j.pk}' if purpose=='photos' else f'worknav:history:{j.pk}:0' if purpose=='history' else f'worknav:job:{j.pk}'
    buttons=[button(job_button_title(j),target(j)) for j in rows[:6]]
    if len(rows)>6:buttons.append(button('Далее',f'worknav:jobs:{purpose}:{section.pk}:{page+1}'))
    if page:buttons.append(button('Назад',f'worknav:jobs:{purpose}:{section.pk}:{page-1}'))
    buttons += [button('Другой участок',f'worknav:sections:{purpose}:0'),button('Меню','/меню')]
    body=section_button_title(section)+'\n'+(f'Выберите работу: {PURPOSES[purpose]}. Работы по возрастанию км.' if rows else 'Нет доступных текущих работ на этом участке.')
    emit(event,binding,body,buttons,purpose,section=str(section.pk),jobs=[str(j.pk) for j in rows[:6]])


def delivery_allowed(binding,scope):
    try:
        rows=jobs_for(binding.user,scope['purpose'],scope.get('contract',''))
        if scope.get('section'):
            if not visible_sections(binding.user).filter(pk=scope['section']).exists():return False
            rows=rows.filter(section_id=scope['section'])
        if 'sections' in scope:
            ids=scope['sections'];return set(ids)=={str(pk) for pk in rows.filter(section_id__in=ids).values_list('section_id',flat=True)}
        ids=scope.get('jobs',[])
        return set(ids)=={str(pk) for pk in rows.filter(pk__in=ids).values_list('pk',flat=True)}
    except (ValueError,ValidationError,KeyError):return False


def _handle(event,binding):
    from .dialog import button,job_title
    text=event.payload.get('text','').strip()
    if not text.startswith('worknav:'):return False
    parts=text.split(':');action=parts[1]
    if action=='sections':sections(event,binding,parts[2],int(parts[3]))
    elif action=='jobs':listing(event,binding,parts[2],uuid.UUID(parts[3]),int(parts[4]))
    elif action=='history':
        job=jobs_for(binding.user,'history',binding.menu_scope.get('contract','')).filter(pk=uuid.UUID(parts[2])).first()
        if not job:raise PermissionDenied
        page=int(parts[3])
        if not 0<=page<=10000:raise ValidationError('Неверная страница.')
        reports=list(job.reports.filter(context__progress__isnull=False).prefetch_related('attachments').order_by('-performed_date','-created_at','pk')[page*5:page*5+6])
        lines=[job_title(job),'Полученные фотоотчёты:']
        for r in reports[:5]:
            lines.append('Фото за '+r.performed_date.strftime('%d.%m.%Y')+f': {r.attachments.count()} шт.'+('\n'+r.note[:350] if r.note else ''))
        from .leadership import link
        photo_link=link('Открыть фотографии в ЛК','repair_detail',[job.pk]);photo_link['url']+='#progress-timeline'
        buttons=[photo_link,button('Работы участка',f'worknav:jobs:history:{job.section_id}:0'),button('Другой участок','worknav:sections:history:0'),button('Добавить фото','/фото')]
        if len(reports)>5:buttons.insert(0,button('Далее',f'worknav:history:{job.pk}:{page+1}'))
        if page:buttons.insert(0,button('Назад',f'worknav:history:{job.pk}:{page-1}'))
        emit(event,binding,'\n\n'.join(lines),buttons,'history',section=str(job.section_id),jobs=[str(job.pk)])
    elif action=='job':
        job=jobs_for(binding.user,'works',binding.menu_scope.get('contract','')).filter(pk=uuid.UUID(parts[2])).first()
        if not job:raise PermissionDenied
        from .progress import available as photos
        from .ddk import available as ddk
        buttons=[]
        if photos(binding.user).filter(pk=job.pk).exists():buttons.append(button('Добавить фото хода работ',f'progress:select:{job.pk}'))
        if ddk(binding.user).filter(pk=job.pk).exists():buttons.append(button('Метод после ДДК',f'ddk:select:{job.pk}'))
        buttons += [button('Расстояния между секциями',f'layout:job:{job.pk}:0'),button('Работы участка',f'worknav:jobs:works:{job.section_id}:0'),button('Другой участок','worknav:sections:works:0')]
        emit(event,binding,job_title(job)+'\nСостояние: '+job.get_state_display(),buttons,'works',section=str(job.section_id),jobs=[str(job.pk)])
    else:raise ValidationError('Откройте меню заново: /меню')
    return True


def handle(event,binding):
    if not event.payload.get('text','').strip().startswith('worknav:'):return False
    try:return _handle(event,binding)
    except (PermissionDenied,ValueError,IndexError,KeyError,TypeError):
        raise ValidationError('Этот список недоступен или устарел. Откройте /меню и выберите участок заново.') from None
