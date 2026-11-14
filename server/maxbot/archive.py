"""Native historical import and read-only completed-repair lookup."""
import uuid
from datetime import timedelta
from django.core.exceptions import ValidationError,PermissionDenied,ObjectDoesNotExist
from django.db.models import Q
from django.utils import timezone
from projects.access import visible_contracts
from repairs.access import visible_jobs
from repairs import archive_import as service
from repairs.models import METHODS
from repairs.historical import result as historical_result
from repairs.values import kilometre
from .models import Draft
from .transport import download_file


def import_state(binding):return binding.menu_scope.get('historical_upload')
def eligible(user):return user.is_active and user.role not in ('customer','subcontractor','lessor')
def completed(user,mode='archive'):
    rows=visible_jobs(user).filter(state='confirmed')
    if mode=='archive':rows=rows.filter(source__historical_import=True)
    return rows

def delivery_allowed(binding,scope):
    if scope.get('batch'):
        if not service.allowed(binding.user):return False
        try:batch=service.batch_for(binding.user,scope['batch'])
        except (PermissionDenied,ValueError,ValidationError):return False
        if batch.payload.get('digest')!=scope.get('digest'):return False
        if scope.get('mode')=='preview':return not batch.applied_at and not batch.payload.get('cancelled') and batch.contract.status=='planned'
        return True
    if not eligible(binding.user):return False
    ids=scope.get('read_jobs',[])
    return set(ids)=={str(pk) for pk in completed(binding.user,'completed').filter(pk__in=ids).values_list('pk',flat=True)}

def emit(event,binding,text,buttons=(),batch=None,jobs=(),mode=''):
    from .dialog import queue
    scope={'archive':True,'read_jobs':[str(j.pk) for j in jobs]}
    if batch:scope.update(batch=str(batch.pk),digest=batch.payload['digest'],mode=mode)
    queue(event,text,buttons,binding=binding,scope=scope)

def state(binding,value):
    scope=binding.menu_scope.copy()
    if value is None:scope.pop('historical_upload',None)
    else:scope['historical_upload']=value
    binding.menu_scope=scope;binding.save(update_fields=['menu_scope'])

def preview(event,binding,batch):
    from .dialog import button
    if batch.applied_at:
        state(binding,None);emit(event,binding,'Этот план уже перенесён. Повтор не добавляет дублей.\n'+service.summary(batch),batch=batch,mode='done');return
    if batch.payload.get('cancelled'):raise ValidationError('Этот предпросмотр отменён. Начните перенос заново.')
    state(binding,{'batch':str(batch.pk),'digest':batch.payload['digest'],'expires':(timezone.now()+timedelta(hours=24)).isoformat()})
    emit(event,binding,'Проверьте перенос:\n'+service.summary(batch)+'\nПосле подтверждения договор перейдёт в архив для просмотра.',[
        button('Показать записи',f'archive:rows:{batch.pk}:0'),
        button('Подтвердить перенос',f'archive:apply:{batch.pk}:{batch.payload["digest"]}'),
        button('Отменить перенос','/отмена')],batch=batch,mode='preview')

def list_rows(event,binding,mode,page=0):
    from .dialog import button,job_title
    if not eligible(binding.user):raise PermissionDenied
    if mode not in ('archive','completed') or not 0<=page<=10000:raise ValidationError('Неверная страница.')
    query=binding.menu_scope.get('archive_query','')
    rows=completed(binding.user,mode)
    if query:
        condition=Q(defects__number__icontains=query)|Q(defects__pipe_section__icontains=query)
        try:
            km=kilometre(query);condition|=Q(defects__kilometre__gte=max(0,km-km.__class__('.0005')),defects__kilometre__lt=km+km.__class__('.0005'))
        except ValidationError:pass
        rows=rows.filter(condition).distinct()
    entries=list(rows.order_by('section__project_object__name','pk')[page*6:page*6+7])
    buttons=[button(job_title(j)[:100],f'archive:job:{j.pk}') for j in entries[:6]]
    if len(entries)>6:buttons.append(button('Далее',f'archive:page:{mode}:{page+1}'))
    if page:buttons.append(button('Назад',f'archive:page:{mode}:{page-1}'))
    buttons += [button('Найти по км, № или секции',f'archive:search:{mode}'),button('Сбросить поиск',f'archive:clear:{mode}')]
    emit(event,binding,('Архив ремонтов' if mode=='archive' else 'Устранённые ремонты')+('\nПоиск: '+query if query else '')+('\nВыберите запись.' if entries else '\nДоступных записей нет.'),buttons,jobs=entries[:6])

def handle(event,binding,stored_keys):
    from .dialog import button,job_title
    text=event.payload.get('text','').strip();files=event.payload.get('photos',[]);active=import_state(binding)
    requested=text.startswith('archive:') or text in ('/архив','/устраненные','/устранённые','/импорт_архива')
    searching=binding.menu_scope.get('archive_search')
    if searching and not requested and not active and (text.startswith('/') or ':' in text or files):
        binding.menu_scope=binding.menu_scope.copy();binding.menu_scope.pop('archive_search',None);binding.save(update_fields=['menu_scope'])
        return False
    if not requested and not active and not (searching and text and not text.startswith('/') and ':' not in text):return False
    try:
        if active and active.get('batch') and text in ('/menu','/меню','/импорт_архива'):
            preview(event,binding,service.batch_for(binding.user,active['batch']));return True
        if active and not requested and text!='/отмена' and not files:
            emit(event,binding,'Есть незавершённый перенос архива. Прикрепите план JSON либо отмените перенос.',[button('Отменить','/отмена')]);return True
        if active and text=='/отмена':
            cancelled=service.cancel(binding.user,active['batch']) if active.get('batch') else True
            state(binding,None);emit(event,binding,'Перенос отменён. Записи не загружены.' if cancelled else 'Перенос уже выполнен. Архив сохранён.');return True
        if searching and not requested and not active:
            if len(text)>150:raise ValidationError('Запрос — до 150 символов.')
            binding.menu_scope=dict(binding.menu_scope,archive_query=text);binding.menu_scope.pop('archive_search',None);binding.save(update_fields=['menu_scope'])
            list_rows(event,binding,searching);return True
        if text in ('/архив','/устраненные','/устранённые'):
            list_rows(event,binding,'archive' if text=='/архив' else 'completed');return True
        parts=text.split(':');action=parts[1] if text.startswith('archive:') else ''
        if text=='/импорт_архива' or action=='contracts':
            if not service.allowed(binding.user):raise PermissionDenied
            page=int(parts[2]) if action else 0
            if not 0<=page<=10000:raise ValidationError('Неверная страница.')
            rows=list(visible_contracts(binding.user).filter(number=service.TITLE,status='planned').order_by('pk')[page*6:page*6+7])
            buttons=[button(c.number,f'archive:upload:{c.pk}') for c in rows[:6]]
            if len(rows)>6:buttons.append(button('Далее',f'archive:contracts:{page+1}'))
            emit(event,binding,'Выберите архивный договор в статусе «Подготовка».' if rows else 'Архивного договора в статусе «Подготовка» нет.',buttons);return True
        if action=='upload':
            if not service.allowed(binding.user):raise PermissionDenied
            if active:raise ValidationError('Сначала завершите или отмените начатый перенос.')
            if Draft.objects.filter(binding=binding,state__in=['editing','photos','confirm']).exists():raise ValidationError('Сначала завершите или отмените начатое действие.')
            contract=visible_contracts(binding.user).get(pk=uuid.UUID(parts[2]),number=service.TITLE,status='planned')
            state(binding,{'contract':str(contract.pk),'expires':(timezone.now()+timedelta(hours=24)).isoformat()})
            emit(event,binding,contract.number+'\nПрикрепите подготовленный JSON-план скрепкой. Размер до 3 МБ. До проверки и подтверждения записи не загружаются.',[button('Отменить','/отмена')]);return True
        if active and files:
            if not service.allowed(binding.user):raise PermissionDenied
            if active.get('batch'):raise ValidationError('Сначала отмените прежний предпросмотр, чтобы заменить план.')
            if len(files)!=1 or files[0].get('type')!='file':raise ValidationError('Прикрепите один файл JSON как документ.')
            if timezone.now()>timezone.datetime.fromisoformat(active['expires']):raise ValidationError('Срок загрузки истёк. Отмените и начните заново.')
            upload=download_file(files[0].get('url'),files[0].get('name'))
            batch=service.prepare(binding.user,uuid.UUID(active['contract']),upload,stored_keys);preview(event,binding,batch);return True
        if action in ('apply','rows','proof'):
            batch=service.batch_for(binding.user,uuid.UUID(parts[2]))
            if action=='apply' and batch.applied_at:
                emit(event,binding,'Этот план уже перенесён. Повтор не добавляет дублей.',batch=batch,mode='done');return True
            if not active or active.get('batch')!=parts[2]:raise ValidationError('Это предыдущий предпросмотр. Откройте текущий перенос.')
            if action=='apply':
                if len(parts)!=4 or parts[3]!=active['digest']:raise ValidationError('Сводка изменилась. Используйте последнее подтверждение.')
                service.apply(binding.user,batch.pk,parts[3]);batch.refresh_from_db()
                # Keep the completed batch pointer for idempotent replay; other commands
                # clear it lazily, without treating a completed import as an active draft.
                state(binding,None)
                emit(event,binding,'Исторический реестр перенесён. Договор в архиве для просмотра.\n'+service.summary(batch),[button('Открыть архив','/архив')],batch=batch,mode='done');return True
            if action=='proof':
                from types import SimpleNamespace
                from repairs.historical import evidence_pages
                index=int(parts[3]);page=int(parts[4])
                if not 0<=index<len(batch.payload['jobs']) or page<0:raise ValidationError('Неверная страница.')
                pages=evidence_pages(SimpleNamespace(source=batch.payload['jobs'][index]['source']))
                if page>=len(pages):raise ValidationError('Неверная страница.')
                buttons=[button('К сводке',f'archive:rows:{batch.pk}:0'),button('Отменить','/отмена')]
                if page+1<len(pages):buttons.insert(0,button('Далее',f'archive:proof:{batch.pk}:{index}:{page+1}'))
                emit(event,binding,'Источники работы '+str(index+1)+'\n'+pages[page],buttons,batch=batch,mode='preview');return True
            page=int(parts[3])
            if not 0<=page<=10000:raise ValidationError('Неверная страница.')
            flat=[(row,defect) for row in batch.payload['jobs'] for defect in row['defects']]
            rows=flat[page*5:page*5+5]
            body='Записи предпросмотра:'
            for row,d in rows:
                body+='\n\n'+row['object']+'\nкм '+d['kilometre']+' №'+d['number']+' секция '+(d['pipe_section'] or 'не указана')+'\n'+('Метод не уточнён' if row['source'].get('actual_method_unknown') else dict(METHODS)[row['fields']['method']])+' · дата '+(row['date'] or 'не указана')
                if len(row['defects'])>1:body+='\nОбщая работа на '+str(len(row['defects']))+' дефектов.'
            indexes=list(dict.fromkeys(batch.payload['jobs'].index(row) for row,_ in rows))
            buttons=[button('Источники работы '+str(i+1),f'archive:proof:{batch.pk}:{i}:0') for i in indexes]
            if (page+1)*5<len(flat):buttons.append(button('Далее',f'archive:rows:{batch.pk}:{page+1}'))
            buttons += [button('Подтвердить перенос',f'archive:apply:{batch.pk}:{batch.payload["digest"]}'),button('Отменить','/отмена')]
            if len(body)>3800:raise ValidationError('Сводка слишком длинная. Используйте основное подтверждение после проверки файла.')
            emit(event,binding,body,buttons,batch=batch,mode='preview');return True
        if action=='page':list_rows(event,binding,parts[2],int(parts[3]));return True
        if action in ('search','clear'):
            mode=parts[2]
            if mode not in ('archive','completed') or not eligible(binding.user):raise PermissionDenied
            scope=binding.menu_scope.copy()
            if action=='search':scope['archive_search']=mode
            else:scope.pop('archive_search',None);scope.pop('archive_query',None)
            binding.menu_scope=scope;binding.save(update_fields=['menu_scope'])
            if action=='search':emit(event,binding,'Введите километр, номер дефекта или номер секции.',[button('Сбросить поиск',f'archive:clear:{mode}')])
            else:list_rows(event,binding,mode)
            return True
        if action in ('job','evidence'):
            job=completed(binding.user,'completed').filter(pk=uuid.UUID(parts[2])).first()
            if not job:raise PermissionDenied
            report=historical_result(job,job.reports.filter(kind__in=['complete','partial','correction']).first())
            from .leadership import contract_name
            day=report.performed_date.strftime('%d.%m.%Y') if report and report.performed_date else 'не указана'
            method=report.get_method_display() if report else 'не указан'
            page=int(parts[3]) if len(parts)>3 else 0
            if not 0<=page<=10000:raise ValidationError('Неверная страница.')
            buttons=[button('Архив','/архив'),button('Устранённые','/устранённые')]
            if action=='evidence':
                from repairs.historical import evidence_pages
                pages=evidence_pages(job)
                if page>=len(pages):raise ValidationError('Страница не найдена.')
                body='Источники исторического ремонта · '+str(page+1)+'/'+str(len(pages))+'\n'+pages[page]
                if page+1<len(pages):buttons.insert(0,button('Далее',f'archive:evidence:{job.pk}:{page+1}'))
                if page:buttons.insert(0,button('Назад',f'archive:evidence:{job.pk}:{page-1}'))
                buttons.insert(0,button('К ремонту',f'archive:job:{job.pk}'))
            else:
                defects=sorted(job.active_defects,key=lambda d:(d.kilometre,d.number))
                rows=defects[page*5:page*5+5]
                if not rows:raise ValidationError('Страница не найдена.')
                body='Контракт: '+contract_name(binding.user,job.contract)+'\n'+job.section.project_object.name
                body+='\n'+'\n'.join(f'км {d.km} · №{d.number} · секция {d.pipe_section or "не указана"}' for d in rows)
                body+=f'\nУстранён. Дата завершения: {day}.\nМетод: {method}.'
                if report and report.length_m is not None:body+='\nДлина конструкции: '+str(report.length_m)+' м.'
                if report and report.composition:body+='\nСостав: '+report.composition
                if len(defects)>1:body+='\nОбщая работа; длина и состав учтены один раз.'
                if (page+1)*5<len(defects):buttons.insert(0,button('Другие дефекты',f'archive:job:{job.pk}:{page+1}'))
                if page:buttons.insert(0,button('Назад',f'archive:job:{job.pk}:{page-1}'))
                if job.source.get('historical_import'):
                    body+='\nИсторическая запись. Отсутствующие даты и замеры не восстанавливались.'
                    buttons.insert(0,button('Источники и монтаж',f'archive:evidence:{job.pk}:0'))
            emit(event,binding,body,buttons,jobs=[job]);return True
        return False
    except (ValidationError,PermissionDenied,ObjectDoesNotExist,ValueError,TypeError,KeyError,IndexError) as e:
        msg='\n'.join(e.messages) if isinstance(e,ValidationError) else 'Действие недоступно или устарело. Откройте архив заново.'
        emit(event,binding,msg[:3000],[button('Отменить','/отмена')] if active else [button('Архив','/архив')]);return True
