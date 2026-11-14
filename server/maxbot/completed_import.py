"""Native upload, preview, evidence, confirmation and cancellation for 2026."""
import uuid
from datetime import timedelta
from django.core.exceptions import ValidationError,PermissionDenied,ObjectDoesNotExist
from django.utils import timezone
from projects.access import visible_contracts
from repairs import completed_import as service
from repairs.models import METHODS
from repairs.historical import evidence_pages
from types import SimpleNamespace
from .transport import download_file
from .models import Draft


def state(binding,value):
    scope=binding.menu_scope.copy()
    if value is None:scope.pop('completed_upload',None)
    else:scope['completed_upload']=value
    binding.menu_scope=scope;binding.save(update_fields=['menu_scope'])


def emit(event,binding,text,buttons=(),batch=None,contract=None):
    from .dialog import queue
    scope={'completed_import':{'contract':str(batch.contract_id if batch else contract.pk) if batch or contract else ''}}
    if batch:scope['completed_import'].update(batch=str(batch.pk),digest=batch.payload['digest'])
    queue(event,text,buttons,binding=binding,scope=scope)


def delivery_allowed(binding,scope):
    try:
        if not service.allowed(binding.user):return False
        if scope.get('contract'):service.target(binding.user,uuid.UUID(scope['contract']))
        if scope.get('batch'):
            b=service.batch_for(binding.user,uuid.UUID(scope['batch']))
            if b.payload['digest']!=scope.get('digest') or b.payload.get('cancelled'):return False
        return True
    except (PermissionDenied,ValidationError,ValueError,ObjectDoesNotExist):return False


def preview(event,binding,batch):
    from .dialog import button
    if batch.applied_at:
        state(binding,None);emit(event,binding,'Этот план уже выполнен.\n'+service.summary(batch),[button('Устранённые','/устранённые')],batch=batch);return
    state(binding,{'contract':str(batch.contract_id),'batch':str(batch.pk),'digest':batch.payload['digest'],
                   'expires':(timezone.now()+timedelta(hours=24)).isoformat()})
    emit(event,binding,'Проверьте перенос:\n'+service.summary(batch),[
        button('Показать записи',f'ci:rows:{batch.pk}:0'),button('Добавить устранённые',f'ci:apply:{batch.pk}:{batch.payload["digest"]}'),button('Отменить','/отмена')],batch=batch)


def handle(event,binding,stored_keys):
    from .dialog import button
    text=event.payload.get('text','').strip();files=event.payload.get('photos',[]);active=binding.menu_scope.get('completed_upload')
    if not (text=='/импорт_2026' or text.startswith('ci:') or active):return False
    try:
        if not service.allowed(binding.user):raise PermissionDenied
        if active and text in ('/отмена','/cancel'):
            if active.get('batch'):service.cancel(binding.user,uuid.UUID(active['batch']))
            state(binding,None);emit(event,binding,'Перенос отменён. Новые записи не добавлены.',[button('Меню','/меню')]);return True
        if active and not text.startswith('ci:') and not files:
            if active.get('batch'):preview(event,binding,service.batch_for(binding.user,uuid.UUID(active['batch'])))
            else:
                c=service.target(binding.user,uuid.UUID(active['contract']))
                emit(event,binding,'Ожидаю подготовленный JSON-план. Прикрепите его скрепкой или отмените перенос.',[button('Отменить','/отмена')],contract=c)
            return True
        if text=='/импорт_2026':
            if active or binding.menu_scope.get('historical_upload') or binding.menu_scope.get('pipe_layout_upload') or Draft.objects.filter(binding=binding,state__in=['editing','photos','confirm']).exists():
                raise ValidationError('Сначала завершите или отмените начатое действие.')
            rows=list(visible_contracts(binding.user).filter(status='active').order_by('number','pk'))
            rows=[c for c in rows if service.NUMBER_TOKEN in c.number.replace('—','-').replace('–','-')][:20]
            emit(event,binding,'Выберите действующий контракт для выполненных дефектов 2026.' if rows else 'Действующий контракт ТЗС-1731-2025 не найден.',
                 [button(c.number[:100],f'ci:upload:{c.pk}') for c in rows]+[button('Меню','/меню')]);return True
        parts=text.split(':');action=parts[1] if text.startswith('ci:') else ''
        if action=='upload':
            if active or binding.menu_scope.get('historical_upload') or binding.menu_scope.get('pipe_layout_upload') or Draft.objects.filter(binding=binding,state__in=['editing','photos','confirm']).exists():raise ValidationError('Сначала завершите или отмените начатое действие.')
            c=service.target(binding.user,uuid.UUID(parts[2]));state(binding,{'contract':str(c.pk),'expires':(timezone.now()+timedelta(hours=24)).isoformat()})
            emit(event,binding,c.number+'\nПрикрепите подготовленный JSON-план выполненных дефектов 2026 скрепкой. До проверки и подтверждения данные не меняются.',[button('Отменить','/отмена')],contract=c);return True
        if active and files:
            if active.get('batch'):raise ValidationError('Сначала завершите или отмените предыдущий предпросмотр.')
            if timezone.now()>timezone.datetime.fromisoformat(active['expires']):raise ValidationError('Срок загрузки истёк. Отмените и начните заново.')
            if len(files)!=1 or files[0].get('type')!='file':raise ValidationError('Прикрепите один JSON-файл как документ.')
            file=download_file(files[0].get('url'),files[0].get('name'))
            preview(event,binding,service.prepare(binding.user,uuid.UUID(active['contract']),file,stored_keys));return True
        batch=service.batch_for(binding.user,uuid.UUID(parts[2]))
        if action=='apply' and batch.applied_at:
            preview(event,binding,batch);return True
        if not active or active.get('batch')!=str(batch.pk):raise ValidationError('Это старый предпросмотр. Откройте текущий перенос.')
        if action=='apply':
            if len(parts)!=4 or parts[3]!=active['digest']:raise ValidationError('Подтверждение изменилось. Откройте сводку заново.')
            service.apply(binding.user,batch.pk,parts[3]);batch.refresh_from_db();state(binding,None)
            emit(event,binding,'Выполненные дефекты добавлены в действующий контракт.\n'+service.summary(batch),[button('Устранённые','/устранённые')],batch=batch);return True
        if action=='rows':
            page=int(parts[3])
            if not 0<=page<=10000:raise ValueError
            entries=list(zip(batch.payload['rows'],batch.payload['classification']))[page*5:page*5+5]
            statuses={'create':'Добавить устранённым','skip':'Уже есть — пропустить','hold':'Совпадение — не менять'}
            body='Записи переноса:'
            buttons=[]
            for index,(r,s) in enumerate(entries,page*5):
                d=r['defect'];body+='\n\n'+r['object']+'\nкм '+d['kilometre']+' · №'+d['number']+' · секция '+(d['pipe_section'] or 'не указана')+'\n'+dict(METHODS)[r['fields']['method']]+' · '+(r['fields']['length_m'] or '—')+' м · дата '+(r['date'] or 'не установлена')+'\n'+statuses[s['status']]
                buttons.append(button('Источники '+str(index+1),f'ci:proof:{batch.pk}:{index}:0'))
            if (page+1)*5<len(batch.payload['rows']):buttons.append(button('Далее',f'ci:rows:{batch.pk}:{page+1}'))
        elif action=='proof':
            index=int(parts[3]);page=int(parts[4])
            if not 0<=index<len(batch.payload['rows']) or page<0:raise ValueError
            pages=evidence_pages(SimpleNamespace(source=batch.payload['rows'][index]['source']))
            if page>=len(pages):raise ValueError
            body=pages[page];buttons=[button('К записям',f'ci:rows:{batch.pk}:0')]
            if page+1<len(pages):buttons.append(button('Далее',f'ci:proof:{batch.pk}:{index}:{page+1}'))
        else:raise ValueError
        buttons += [button('Добавить устранённые',f'ci:apply:{batch.pk}:{batch.payload["digest"]}'),button('Отменить','/отмена')]
        emit(event,binding,body[:3800],buttons,batch=batch);return True
    except (PermissionDenied,ValidationError,ObjectDoesNotExist,ValueError,TypeError,KeyError,IndexError) as e:
        msg='\n'.join(e.messages) if isinstance(e,ValidationError) else 'Перенос недоступен или устарел. Начните /импорт_2026 заново.'
        emit(event,binding,msg[:3000],[button('Отменить','/отмена')] if active else [button('Меню','/меню')]);return True
