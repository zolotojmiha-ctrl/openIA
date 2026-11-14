"""Full native XLSX upload/confirmation and section-start distances."""
import uuid,json,hashlib
from datetime import timedelta
from django.core.exceptions import PermissionDenied,ValidationError,ObjectDoesNotExist
from django.utils import timezone
from django.db.models import Min,Q
from repairs import layouts as service
from repairs.models import PipeLayout,Defect
from repairs.access import can_prepare,visible_sections,visible_jobs
from .models import Draft
from .transport import download_file
from .labels import section_button_title


def current_jobs(user):
    rows=visible_jobs(user).exclude(contract__status='archived').exclude(state__in=['confirmed','reported','unrepairable'])
    return rows.none() if user.role in ('customer','lessor') and not user.is_leader else rows


def snapshot(user,job):
    defects=sorted(job.active_defects,key=lambda d:(d.kilometre,d.number,str(d.pk)))
    found=service.neighbours(user,defects);signature=[];pairs=[]
    for d in defects:
        for pair in found.get(d.pk,[]):
            signature.append([str(d.pk),pair['direction'],str(pair['left'].pk),str(pair['right'].pk),
                pair['left'].pipe_section,pair['right'].pipe_section,str(pair['left'].kilometre),str(pair['right'].kilometre),
                str(pair.get('metres','')),pair.get('reason',''),str(pair['layout'].pk) if pair['layout'] else ''])
            if not any(p['left'].pk==pair['left'].pk and p['right'].pk==pair['right'].pk for p in pairs):pairs.append(pair)
    return pairs,hashlib.sha256(json.dumps(signature,ensure_ascii=False).encode()).hexdigest()

def pair_text(pair):
    text=f'Начало секции {pair["left"].pipe_section or "не указана"} → начало секции {pair["right"].pipe_section or "не указана"}: '
    if pair.get('reason'):return text+pair['reason']
    return text+pair['text']+' м.'+(' Более 6 км: может потребоваться трал. Уточните дорожный маршрут.' if pair['trailer'] else '')

def delivery_allowed(binding,scope):
    if scope.get('info'):return True
    if scope.get('reader_job'):
        if binding.user.role in ('customer','lessor') and not binding.user.is_leader:return False
        job=visible_jobs(binding.user).filter(pk=scope['reader_job']).first()
        if not job:return False
        _,signature=snapshot(binding.user,job)
        return signature==scope.get('signature')
    if not service.manage_allowed(binding.user):return False
    if scope.get('sections') is not None:
        ids=scope['sections']
        if set(ids)!={str(pk) for pk in visible_sections(binding.user).filter(pk__in=ids).values_list('pk',flat=True)}:return False
    if scope.get('section') and not visible_sections(binding.user).filter(pk=scope['section']).exists():return False
    if scope.get('layout'):
        row=service.scoped(binding.user,scope['layout'])
        if not row:return False
        if scope.get('preview'):return row.author_id==binding.user_id and row.session_version==binding.user.session_version and row.state=='preview'
    return True

def queue(event,binding,text,buttons=(),**scope):
    from .dialog import queue as emit
    emit(event,text,buttons,binding=binding,scope={'pipe_layout':True,**scope})

def state(binding,value):
    scope=binding.menu_scope.copy();scope.pop('archive_search',None)
    if value is None:scope.pop('pipe_layout_upload',None)
    else:scope['pipe_layout_upload']=value
    binding.menu_scope=scope;binding.save(update_fields=['menu_scope'])

def preview(event,binding,row):
    from .dialog import button
    if row.state=='active':
        state(binding,None);queue(event,binding,'Эта раскладка уже используется. Повтор не создаёт новую действующую версию.',section=str(row.section_id),layout=str(row.pk));return
    if row.state!='preview':raise ValidationError('Предпросмотр больше не действует. Начните загрузку заново.')
    stats=service.status(row)
    state(binding,{'layout':str(row.pk),'section':str(row.section_id)})
    queue(event,binding,f'{row.section.project_object.name} · {row.section.pipeline}\n{row.filename} · лист {row.sheet}\nСекций: {stats["rows"]}. Разрывов или неполных переходов: {stats["breaks"]}. Повторяющихся номеров: {stats["duplicates"]}.\nРасстояния будут доступны только по непрерывной цепочке. Дефекты, номера секций и остатки не меняются. Прежняя версия сохранится.',
        [button('Первые секции',f'layout:sample:{row.pk}'),button('Использовать раскладку',f'layout:apply:{row.pk}'),button('Отменить','/отмена')],section=str(row.section_id),layout=str(row.pk),preview=True)

def handle(event,binding,stored_keys):
    from .dialog import button,job_title,job_button_title
    text=event.payload.get('text','').strip();files=event.payload.get('photos',[]);active=binding.menu_scope.get('pipe_layout_upload')
    requested=text.startswith('layout:') or text in ('/раскладка','/расстояния')
    if not active and not requested:return False
    if requested and binding.menu_scope.get('archive_search'):
        scope=binding.menu_scope.copy();scope.pop('archive_search',None);binding.menu_scope=scope;binding.save(update_fields=['menu_scope'])
    try:
        if binding.user.role in ('customer','lessor') and not binding.user.is_leader:raise PermissionDenied
        if active and text=='/отмена':
            applied=False
            if active.get('layout') and service.manage_allowed(binding.user):
                try:applied=service.cancel(binding.user,active['layout'])=='active'
                except PermissionDenied:pass
            state(binding,None);queue(event,binding,'Раскладка уже применена; версия сохранена.' if applied else 'Загрузка раскладки отменена. Действующая версия сохранена.');return True
        if active and text in ('/menu','/меню','/раскладка') and active.get('layout'):
            row=service.scoped(binding.user,active['layout'])
            if not row or row.author_id!=binding.user_id or not service.manage_allowed(binding.user):raise PermissionDenied
            preview(event,binding,row);return True
        if active and files:
            if not service.manage_allowed(binding.user):raise PermissionDenied
            if active.get('layout'):raise ValidationError('Сначала отмените прежний предпросмотр, чтобы заменить файл.')
            if timezone.now()>timezone.datetime.fromisoformat(active['expires']):raise ValidationError('Срок загрузки истёк. Отмените загрузку и начните заново.')
            if len(files)!=1 or files[0].get('type')!='file':raise ValidationError('Прикрепите одну раскладку XLSX как документ.')
            upload=download_file(files[0].get('url'),files[0].get('name'))
            row=service.prepare(binding.user,uuid.UUID(active['section']),upload,stored_keys);preview(event,binding,row);return True
        if active and not requested:
            queue(event,binding,'Есть незавершённая загрузка раскладки. Прикрепите XLSX или отмените.',[button('Отменить','/отмена')]);return True
        parts=text.split(':');action=parts[1] if text.startswith('layout:') else ''
        if text=='/раскладка' or action=='sections':
            if not service.manage_allowed(binding.user):raise PermissionDenied
            page=int(parts[2]) if action else 0
            if not 0<=page<=10000:raise ValueError
            rows=list(visible_sections(binding.user).order_by('project_object__name','pipeline','pk')[page*6:page*6+7])
            buttons=[button(section_button_title(s),f'layout:upload:{s.pk}') for s in rows[:6]]
            if len(rows)>6:buttons.append(button('Далее',f'layout:sections:{page+1}'))
            if page:buttons.append(button('Назад',f'layout:sections:{page-1}'))
            queue(event,binding,'Выберите участок нефтепровода для раскладки.' if rows else 'Нет доступных участков.',buttons,sections=[str(s.pk) for s in rows[:6]]);return True
        if action=='upload':
            if not service.manage_allowed(binding.user):raise PermissionDenied
            if active or binding.menu_scope.get('historical_upload') or Draft.objects.filter(binding=binding,state__in=['editing','photos','confirm']).exists():raise ValidationError('Сначала завершите или отмените начатое действие.')
            section=visible_sections(binding.user).get(pk=uuid.UUID(parts[2]))
            state(binding,{'section':str(section.pk),'expires':(timezone.now()+timedelta(hours=24)).isoformat()})
            queue(event,binding,f'{section.project_object.name} · {section.pipeline}\nПрикрепите раскладку XLSX скрепкой, до 10 МБ. Сначала будет проверка, затем подтверждение.',[button('Версии раскладки',f'layout:versions:{section.pk}:0'),button('Отменить','/отмена')],section=str(section.pk));return True
        if action=='versions':
            if not service.manage_allowed(binding.user):raise PermissionDenied
            section=visible_sections(binding.user).get(pk=uuid.UUID(parts[2]));page=int(parts[3])
            if not 0<=page<=10000:raise ValueError
            rows=list(PipeLayout.objects.filter(section=section,state__in=['active','superseded']).order_by('-applied_at','pk')[page*5:page*5+6])
            buttons=[button((r.filename+' · '+r.get_state_display())[:100],f'layout:version:{r.pk}') for r in rows[:5]]
            if len(rows)>5:buttons.append(button('Далее',f'layout:versions:{section.pk}:{page+1}'))
            if page:buttons.append(button('Назад',f'layout:versions:{section.pk}:{page-1}'))
            queue(event,binding,'Сохранённые версии: '+section.project_object.name if rows else 'Сохранённых версий ещё нет.',buttons,section=str(section.pk));return True
        if action=='version':
            if not service.manage_allowed(binding.user):raise PermissionDenied
            row=service.scoped(binding.user,uuid.UUID(parts[2]))
            if not row or row.state not in ('active','superseded'):raise PermissionDenied
            stats=service.status(row)
            body=f'{row.filename} · {row.get_state_display()}\nЛист: {row.sheet}. Секций: {stats["rows"]}; неполных переходов: {stats["breaks"]}.'
            for r in row.data['rows'][:5]:body+=f'\n№ {r[0]} · начало {r[1] or "не указано"} м · длина {r[2] or "не указана"} м'
            queue(event,binding,body,[button('Все версии',f'layout:versions:{row.section_id}:0')],section=str(row.section_id),layout=str(row.pk));return True
        if action in ('apply','sample'):
            if not service.manage_allowed(binding.user):raise PermissionDenied
            row=service.scoped(binding.user,uuid.UUID(parts[2]))
            if not row:raise PermissionDenied
            if row.author_id!=binding.user_id:raise PermissionDenied
            if action=='apply' and row.state=='active':
                queue(event,binding,'Эта версия уже используется. Повтор безопасен.',section=str(row.section_id),layout=str(row.pk));return True
            if not active or active.get('layout')!=str(row.pk):raise ValidationError('Используйте последний предпросмотр.')
            if action=='apply':
                selected=service.apply(binding.user,row.pk);state(binding,None)
                queue(event,binding,'Раскладка выбрана. Расстояния доступны в карточках работ и по команде /расстояния. Дефекты и остатки сохранены.',[button('Расстояния','/расстояния')],section=str(selected.section_id),layout=str(selected.pk));return True
            body='Первые секции источника:'
            for r in row.data['rows'][:5]:body+=f'\n№ {r[0]} · начало {r[1] or "не указано"} м · длина {r[2] or "не указана"} м · следующая {r[3] or "не указана"}'
            queue(event,binding,body,[button('Использовать раскладку',f'layout:apply:{row.pk}'),button('Отменить','/отмена')],section=str(row.section_id),layout=str(row.pk),preview=True);return True
        if text=='/расстояния' or action=='worksections' or action=='jobs' and len(parts)==3:
            page=int(parts[2]) if action=='worksections' else 0
            if not 0<=page<=10000:raise ValueError
            sections=list(visible_sections(binding.user).filter(pk__in=current_jobs(binding.user).values('section_id')).select_related('project_object').order_by('project_object__name','pipeline','pk')[page*6:page*6+7])
            buttons=[button(section_button_title(s),f'layout:jobs:{s.pk}:0') for s in sections[:6]]
            if len(sections)>6:buttons.append(button('Далее',f'layout:worksections:{page+1}'))
            if page:buttons.append(button('Назад',f'layout:worksections:{page-1}'))
            from .dialog import queue as emit
            emit(event,'Выберите участок с текущими работами. Затем выберите работу для просмотра расстояний между началами секций.' if sections else 'Нет доступных текущих работ.',buttons,binding=binding,scope={'layout_work_sections':[str(s.pk) for s in sections[:6]]});return True
        if action=='jobs':
            section=visible_sections(binding.user).get(pk=uuid.UUID(parts[2]));page=int(parts[3])
            if not 0<=page<=10000:raise ValueError
            jobs=list(current_jobs(binding.user).filter(section=section).annotate(button_km=Min('defects__kilometre',filter=Q(defects__archived_at__isnull=True))).order_by('button_km','pk')[page*6:page*6+7])
            buttons=[button(job_button_title(j),f'layout:job:{j.pk}:0') for j in jobs[:6]]
            if len(jobs)>6:buttons.append(button('Далее',f'layout:jobs:{section.pk}:{page+1}'))
            if page:buttons.append(button('Назад',f'layout:jobs:{section.pk}:{page-1}'))
            buttons.append(button('Другой участок','/расстояния'))
            from .dialog import queue as emit
            emit(event,section.project_object.name+'\n'+('Выберите работу: расстояния между началами соседних секций.' if jobs else 'Нет доступных текущих работ на этом участке.'),buttons,binding=binding,scope={'layout_jobs':[str(j.pk) for j in jobs[:6]],'layout_selected_section':str(section.pk)});return True
        if action=='job':
            job=visible_jobs(binding.user).filter(pk=uuid.UUID(parts[2])).first()
            if not job:raise PermissionDenied
            page=int(parts[3]) if len(parts)>3 else 0
            if not 0<=page<=10000:raise ValueError
            pairs,signature=snapshot(binding.user,job)
            body=job_title(job)+'\nРасстояния вдоль трубы между началами секций. Дорожный маршрут уточняется отдельно.'
            for p in pairs[page*3:page*3+3]:body+='\n\n'+pair_text(p)+('\nРаскладка: '+p['layout'].filename if p['layout'] else '')
            if not pairs:body+='\nНет доступных соседних дефектов.'
            buttons=[button('Другие работы участка',f'layout:jobs:{job.section_id}:0'),button('Другой участок','/расстояния')]
            if (page+1)*3<len(pairs):buttons.insert(0,button('Далее',f'layout:job:{job.pk}:{page+1}'))
            if page:buttons.insert(0,button('Назад',f'layout:job:{job.pk}:{page-1}'))
            queue(event,binding,body,buttons,reader_job=str(job.pk),signature=signature);return True
        return False
    except (ValidationError,PermissionDenied,ObjectDoesNotExist,ValueError,KeyError,IndexError,TypeError) as e:
        body='\n'.join(e.messages) if isinstance(e,ValidationError) else 'Действие недоступно или устарело. Выберите участок заново.'
        queue(event,binding,body[:3000],[button('Отменить','/отмена')] if active else [button('Расстояния','/расстояния')],info=True);return True
