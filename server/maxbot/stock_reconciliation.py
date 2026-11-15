"""Native owner review of missing installation stock postings."""
from datetime import timedelta
import uuid
from django.core.exceptions import PermissionDenied,ValidationError
from django.utils import timezone
from repairs import stock_reconciliation as service
from repairs.installation_stock import candidates,source_key
from repairs.models import Coupling
from .models import Draft


def emit(event,binding,draft,text,buttons=()):
    from .dialog import queue
    queue(event,text,buttons,binding=binding,job=draft.job if draft else None,
          scope={'stockfix':{'draft':str(draft.pk),'state':draft.state}} if draft else {'stockfix':{}},suffix='stockfix')


def prompt(event,binding,draft,page=0):
    from .dialog import button
    job=service.allowed(binding.user).get(pk=draft.job_id)
    if draft.step>=len(draft.fields):
        sources={key:draft.raw[key] for key in draft.fields}
        facts,token=service.preview(binding.user,job.pk,sources)
        draft.raw['preview']=token;draft.state='confirm';draft.save(update_fields=['raw','state'])
        lines=['Проверьте недостающее списание:']
        for row in facts['rows']:
            lines.append(f'№{row["serial"]} · {row["method_label"]} · {row["length_mm"]} × {row["wall_mm"]} мм · 1 шт.\nСклад: {row["warehouse_name"]}')
            if row['previous_job'] and row['previous_job']!=str(job.pk):
                from repairs.models import Job
                donor=Job.objects.get(pk=row['previous_job'])
                lines.append('Резерв снят с км '+', '.join(d.km for d in donor.active_defects)+'. По прежней работе потребуется замена.')
        text='\n\n'.join(lines)
        if len(text)>3500:raise ValidationError('Слишком много изделий для одной сверки. Требуется отдельная проверка состава.')
        emit(event,binding,draft,text,[button('Подтвердить списание',f'stockfix:confirm:{draft.pk}'),button('Выбрать заново',f'stockfix:select:{job.pk}'),button('Отменить',f'stockfix:cancel:{draft.pk}')]);return
    coupling=Coupling.objects.get(pk=draft.fields[draft.step])
    rows=candidates(job,coupling.method,coupling.length_mm,coupling.wall_mm,coupling.serial)
    if not 0<=page<=max(0,(len(rows)-1)//6):raise ValidationError('Неверная страница.')
    buttons=[]
    for row in rows[page*6:page*6+6]:
        donor=row.item.reserved_job
        place='резерв км '+', '.join(d.km for d in donor.active_defects) if donor and donor.pk!=job.pk else 'свободно / свой резерв'
        buttons.append(button(f'{row.warehouse.name} · {place} · {row.item.sku}',f'stockfix:source:{draft.pk}:{draft.step}:{source_key(row.item_id,row.warehouse_id)}'))
    if page:buttons.append(button('Назад',f'stockfix:page:{draft.pk}:{draft.step}:{page-1}'))
    if (page+1)*6<len(rows):buttons.append(button('Далее',f'stockfix:page:{draft.pk}:{draft.step}:{page+1}'))
    buttons.append(button('Отменить',f'stockfix:cancel:{draft.pk}'))
    emit(event,binding,draft,f'Выберите складское изделие для №{coupling.serial}: {coupling.get_method_display()} {coupling.length_mm} × {coupling.wall_mm} мм.\n'+('Совместимого остатка нет.' if not rows else 'Перед списанием будет показана сводка.'),buttons)


def handle(event,binding):
    text=event.payload.get('text','')
    if text=='/отмена' and Draft.objects.filter(binding=binding,action='stockfix',state__in=['editing','confirm']).exists():
        Draft.objects.filter(binding=binding,action='stockfix',state__in=['editing','confirm']).update(state='cancelled')
        emit(event,binding,None,'Сверка отменена. Склад не изменён.');return True
    if text!='/сверка_муфт' and not text.startswith('stockfix:'):return False
    from .dialog import button
    service.allowed(binding.user)
    if text=='/сверка_муфт' or text.startswith('stockfix:list:'):
        page=int(text.rsplit(':',1)[-1]) if text.startswith('stockfix:list:') else 0
        if not 0<=page<=10000:raise ValidationError('Неверная страница.')
        jobs=[j for j in service.allowed(binding.user).order_by('pk') if service.pieces(j)]
        from .dialog import job_button_title
        buttons=[button(job_button_title(j),f'stockfix:select:{j.pk}') for j in jobs[page*6:page*6+6]]
        if page:buttons.append(button('Назад',f'stockfix:list:{page-1}'))
        if (page+1)*6<len(jobs):buttons.append(button('Далее',f'stockfix:list:{page+1}'))
        buttons.append(button('Меню','/меню'))
        emit(event,binding,None,'Сверка расхода муфт. Выберите сохранённый ремонт без складского списания.' if jobs else 'Сохранённых ремонтов, требующих сверки, нет.',buttons);return True
    parts=text.split(':');action=parts[1]
    if action=='select':
        job=service.allowed(binding.user).filter(pk=uuid.UUID(parts[2])).first()
        if not job:raise PermissionDenied
        pieces=service.pieces(job)
        if not pieces:raise ValidationError('Недостающих списаний нет.')
        Draft.objects.filter(binding=binding,action='stockfix',state__in=['editing','confirm']).update(state='cancelled')
        draft=Draft.objects.create(binding=binding,job=job,action='stockfix',raw={},fields=[str(c.pk) for c in pieces],expires=timezone.now()+timedelta(minutes=10))
        prompt(event,binding,draft);return True
    draft=Draft.objects.select_for_update().filter(pk=uuid.UUID(parts[2]),binding=binding,action='stockfix').first()
    if not draft:raise PermissionDenied
    if action=='cancel':
        if draft.state!='submitted':draft.state='cancelled';draft.save(update_fields=['state'])
        emit(event,binding,draft,'Сверка завершена без новых изменений.');return True
    if action=='confirm' and draft.state=='submitted':
        emit(event,binding,draft,'Списание уже проведено. Повторного расхода нет.');return True
    if draft.expires<=timezone.now() or draft.state not in ('editing','confirm'):raise ValidationError('Сверка устарела. Откройте /сверка_муфт заново.')
    if action=='confirm' and draft.state=='confirm':
        service.confirm(binding.user,draft.raw['preview']);draft.state='submitted';draft.save(update_fields=['state'])
        emit(event,binding,draft,'Недостающее списание проведено. Прежний отчёт и монтаж сохранены.');return True
    if action in ('source','page') and (draft.state!='editing' or int(parts[3])!=draft.step):raise ValidationError('Кнопка от предыдущего изделия устарела.')
    if action=='source' and draft.state=='editing':
        coupling=Coupling.objects.get(pk=draft.fields[draft.step])
        rows=candidates(draft.job,coupling.method,coupling.length_mm,coupling.wall_mm,coupling.serial)
        if str(parts[4]) not in {source_key(row.item_id,row.warehouse_id) for row in rows}:raise ValidationError('Источник изменился. Откройте сверку заново.')
        draft.raw[draft.fields[draft.step]]=parts[4];draft.step+=1;draft.save(update_fields=['raw','step'])
        prompt(event,binding,draft);return True
    if action=='page' and draft.state=='editing':prompt(event,binding,draft,int(parts[4]));return True
    raise ValidationError('Кнопка сверки устарела.')


def delivery_allowed(binding,scope):
    if not binding.user.is_active or not binding.user.is_leader:return False
    if not scope:return True
    draft=Draft.objects.filter(pk=scope['draft'],binding=binding,action='stockfix',state=scope['state']).first()
    return bool(draft and service.allowed(binding.user).filter(pk=draft.job_id).exists())
