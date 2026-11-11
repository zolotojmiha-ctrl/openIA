"""Structured master conversation. Every report is validated by ReportForm again."""
import re
import uuid
from datetime import date, timedelta
from django import forms
from django.db import transaction
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from django.utils.datastructures import MultiValueDict
from documents.storage import validate_upload, store_file, verified_file, private_path
from repairs.access import reportable_jobs
from repairs.forms import ReportForm
from repairs.services import report_work
from .models import Draft, Photo, Outbox
from .transport import download_photo


def button(text, payload):
    return {'type': 'callback', 'text': text[:100], 'payload': payload}


def queue(event, text, buttons=(), *, binding=None, job=None, scope=None, suffix='reply'):
    body = {'text': text[:4000]}
    if buttons:
        body['attachments'] = [{'type': 'inline_keyboard', 'payload': {'buttons': [[b] for b in buttons]}}]
    Outbox.objects.get_or_create(key=f'in:{event.pk}:{suffix}', defaults={
        'binding': binding, 'job': job, 'max_user': event.max_user, 'chat': event.chat,
        'body': body, 'scope': scope or {}, 'available_at': timezone.now(), 'expires': timezone.now() + timedelta(minutes=30)})


def available(user):
    return reportable_jobs(user).exclude(state__in=['reported', 'confirmed', 'unrepairable'])


def job_title(job):
    defects = ', '.join(f'№{d.number} · км {d.km}' for d in job.active_defects)
    return f'{job.section.project_object.name}\n{job.section.name}\n{defects or "Ремонт"}\n{job.get_method_display()} · {job.length_m or "—"} м'


def form_for(draft, user, files=None):
    return ReportForm(draft.raw, files, job=draft.job, action=draft.action, user=user)


def fields_for(form):
    needs_note = (form.simple and form.action == 'problem' and
                  form.data.get('problem') in ('material_other', 'other', 'existing_coupling', 'oversize_p1'))
    return [name for name, field in form.fields.items()
            if not field.widget.is_hidden and name != 'files' and (name != 'note' or needs_note)]


def report_date(value):
    """Normalize MAX input; retain ISO for existing draft/default values."""
    value = str(value).strip()
    try:
        if re.fullmatch(r'\d{2}\.\d{2}\.\d{2}', value):
            day, month, year = map(int, value.split('.'))
            return date(2000 + year, month, day)
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return date.fromisoformat(value)
    except ValueError:
        pass
    raise ValidationError('Введите существующую дату в формате ДД.ММ.ГГ, например 11.11.26.')


def start(binding, job, action):
    if not available(binding.user).filter(pk=job.pk).exists():
        raise PermissionDenied
    Draft.objects.filter(binding=binding, state__in=['editing', 'photos', 'confirm']).update(state='cancelled')
    form = ReportForm(job=job, action=action, user=binding.user)
    raw = {name: str(value) if value is not None else '' for name, value in form.initial.items()
           if name in form.fields and form.fields[name].widget.is_hidden}
    raw['token'] = str(uuid.uuid4())
    raw['expected_version'] = job.version
    return Draft.objects.create(binding=binding, job=job, action=action, raw=raw,
                fields=fields_for(form), expires=timezone.now() + timedelta(hours=2))


def load_files(draft):
    result = []
    for photo in draft.photos.all():
        with verified_file(photo) as content:
            result.append(SimpleUploadedFile(photo.name, content.read(10 * 1024 * 1024 + 1)))
    return MultiValueDict({'files': result})


def validate(draft, user):
    form = form_for(draft, user, load_files(draft))
    if not form.is_valid():
        errors = []
        for name, messages in form.errors.items():
            label = form.fields[name].label if name in form.fields else 'Отчёт'
            errors.extend(f'{label}: {message}' for message in messages)
        raise ValidationError(errors)
    return form


def prompt(event, binding, draft):
    form = form_for(draft, binding.user)
    if draft.step >= len(draft.fields):
        draft.state = 'photos'
        draft.save(update_fields=['state'])
        queue(event, 'Добавьте фотографии PNG/JPEG (до 10 МБ каждая, всего до 40 МБ и 20 фото). '
              'Либо нажмите «Проверить отчёт». Фото можно добавить и в ЛК.\n'
              f'Сейчас фотографий: {draft.photos.count()}. Для удаления всех фото: /удалить_фото',
              [button('Проверить отчёт', f'review:{draft.pk}')], binding=binding, job=draft.job)
        return
    name = draft.fields[draft.step]
    field = form.fields[name]
    label = str(field.label)
    if name.startswith('serial_'):
        label = 'Введите заводской номер ремонтной конструкции\n' + label.rsplit(' — ', 1)[0]
    elif name == 'manual_serials':
        label = 'Введите заводской номер ремонтной конструкции'
    choices = list(field.choices) if hasattr(field, 'choices') else []
    buttons = [button(str(label), f'answer:{draft.pk}:{draft.step}:{value}') for value, label in choices if value != '']
    initial = draft.raw.get(name, form.initial.get(name, field.initial))
    if initial is not None and not choices and str(initial):
        initial_label = report_date(initial).strftime('%d.%m.%y') if isinstance(field, forms.DateField) else initial
        buttons.append(button(f'Оставить: {initial_label}', f'default:{draft.pk}:{draft.step}'))
    if not field.required and name != 'note':
        buttons.append(button('Пропустить', f'skip:{draft.pk}:{draft.step}'))
    queue(event, f'{job_title(draft.job)}\n\n{draft.step + 1}/{len(draft.fields)}. {label}'
          + ('\nВставьте координаты из Google карт или Яндекс карт.\nОтправьте координаты обычным сообщением.'
             if name == 'coordinates' else '\n' + str(field.help_text) if field.help_text else '')
          + ('\nВведите дату в формате ДД.ММ.ГГ.' if isinstance(field, forms.DateField) else '')
          + ('\nНомер указан на маркировке или в паспорте. Отправьте его обычным сообщением.'
             if name.startswith('serial_') or name == 'manual_serials' else '')
          + ('\nУточните выбранную причину невыполнения работы.' if name == 'note' else '')
          + '\nДля отмены: /отмена', buttons, binding=binding, job=draft.job)


def summarize(event, binding, draft):
    form = validate(draft, binding.user)
    lines = [job_title(draft.job), '', 'Проверьте перед отправкой:']
    for name in draft.fields:
        field = form.fields[name]
        value = draft.raw.get(name, '')
        if value and isinstance(field, forms.DateField):
            value = report_date(value).strftime('%d.%m.%y')
        if hasattr(field, 'choices'):
            value = dict(field.choices).get(value, value)
        lines.append(f'{field.label}: {value or "—"}')
    lines.append(f'Фотографий: {draft.photos.count()}')
    # Never silently truncate the preview of a report the employee confirms.
    text = '\n'.join(lines)
    if len(text) > 3900:
        raise ValidationError('Отчёт слишком длинный для сводки MAX. Сократите пояснение или отправьте через ЛК.')
    draft.state = 'confirm'
    draft.save(update_fields=['state'])
    queue(event, text, [button('Отправить на проверку', f'confirm:{draft.pk}'),
                        button('Исправить', f'edit:{draft.pk}'), button('Отменить', '/отмена')],
          binding=binding, job=draft.job)


def handle(event, binding, stored_keys):
    user = binding.user
    text = event.payload.get('text', '').strip()
    photos = event.payload.get('photos', [])
    if text in ('/start', '/работы', 'Мои работы', '/мои_работы') or text.startswith('sections:') or event.payload['kind'] == 'bot_started':
        page = int(text.split(':')[1]) if text.startswith('sections:') else 0
        if not 0 <= page <= 10000:
            raise ValidationError('Неверная страница.')
        sections = list(available(user).order_by('section_id').values_list('section_id', 'section__name', 'section__project_object__name').distinct()[page * 8:page * 8 + 9])
        buttons = [button(f'{obj} · {name}', f'section:{pk}:0') for pk, name, obj in sections[:8]]
        if len(sections) > 8:
            buttons.append(button('Следующие участки', f'sections:{page + 1}'))
        if page:
            buttons.append(button('К началу списка', '/работы'))
        queue(event, 'Выберите участок. Показаны только назначенные вам работы.' if sections else 'Назначенных работ для отчёта нет.',
              buttons, binding=binding, scope={'sections': [str(pk) for pk, name, obj in sections[:8]]})
        return
    if text.startswith('section:'):
        _, key, page = text.split(':')
        key = uuid.UUID(key)
        page = int(page)
        if not 0 <= page <= 10000:
            raise ValidationError('Неверная страница.')
        jobs = list(available(user).filter(section_id=key).order_by('pk')[page * 8:page * 8 + 9])
        buttons = [button(', '.join('№' + d.number for d in j.active_defects) or 'Ремонт', f'job:{j.pk}') for j in jobs[:8]]
        if len(jobs) > 8:
            buttons.append(button('Следующие работы', f'section:{key}:{page + 1}'))
        buttons.append(button('Другой участок', '/работы'))
        queue(event, '\n\n'.join(job_title(j) for j in jobs[:8]) or 'Доступных работ нет.', buttons, binding=binding,
              scope={'jobs': [str(j.pk) for j in jobs[:8]]})
        return
    if text.startswith('job:'):
        job = available(user).filter(pk=uuid.UUID(text.split(':')[1])).first()
        if not job:
            raise PermissionDenied
        queue(event, job_title(job), [button('Работа выполнена', f'begin:{job.pk}:repaired'),
                button('Не выполнена / препятствие', f'begin:{job.pk}:unrepaired')], binding=binding, job=job)
        return
    if text.startswith('begin:'):
        _, pk, action = text.split(':')
        if action not in ('repaired', 'unrepaired'):
            raise PermissionDenied
        job = available(user).filter(pk=uuid.UUID(pk)).first()
        if not job:
            raise PermissionDenied
        prompt(event, binding, start(binding, job, action))
        return
    if text == '/отмена':
        Draft.objects.filter(binding=binding).exclude(state='submitted').update(state='cancelled')
        queue(event, 'Черновик отменён. Для выбора работы: /работы', binding=binding)
        return
    parts = text.split(':')
    action = parts[0]
    if action in ('review', 'confirm', 'edit', 'answer', 'default', 'skip'):
        draft = Draft.objects.select_for_update().filter(pk=uuid.UUID(parts[1]), binding=binding).first()
    else:
        draft = Draft.objects.select_for_update().filter(binding=binding, state__in=['editing', 'photos', 'confirm']).order_by('-created_at').first()
    if not draft:
        queue(event, 'Для выбора задания нажмите «Мои работы».', [button('Мои работы', '/работы')], binding=binding)
        return
    if not reportable_jobs(user).filter(pk=draft.job_id).exists():
        raise PermissionDenied
    if draft.state == 'submitted' and action == 'confirm':
        queue(event, 'Этот отчёт уже принят платформой. Повторной записи нет.', binding=binding, job=draft.job)
        return
    if draft.state == 'cancelled' or draft.expires <= timezone.now():
        raise ValidationError('Черновик истёк или отменён. Выберите работу заново: /работы')
    if draft.job.version != int(draft.raw['expected_version']):
        raise ValidationError('Работу уже изменили. Черновик сохранён, но отправить его нельзя. Проверьте карточку в ЛК.')
    if fields_for(form_for(draft, user)) != draft.fields:
        raise ValidationError('Состав полей изменился. Откройте актуальную карточку в ЛК.')
    if action == 'edit':
        draft.step = 0
        draft.state = 'editing'
        draft.save(update_fields=['step', 'state'])
        prompt(event, binding, draft)
    elif action == 'confirm':
        if draft.state != 'confirm':
            raise ValidationError('Сначала проверьте сводку отчёта.')
        form = validate(draft, user)
        report = report_work(user, draft.job_id, 'unrepaired' if draft.action == 'unrepaired' else form.action, form.cleaned_data)
        stored_keys.extend(report.attachments.values_list('storage_key', flat=True))
        draft.report = report
        draft.state = 'submitted'
        draft.save(update_fields=['report', 'state'])
        queue(event, 'Отчёт сохранён в платформе и передан на проверку. Для других работ: /работы', binding=binding, job=draft.job)
    elif action == 'review':
        if draft.state != 'photos':
            raise ValidationError('Сначала заполните все поля отчёта.')
        summarize(event, binding, draft)
    elif photos:
        if draft.state != 'photos':
            raise ValidationError('Фотографии добавляются после заполнения полей, перед проверкой отчёта.')
        total = sum(draft.photos.values_list('size', flat=True))
        if draft.photos.count() + len(photos) > 20:
            raise ValidationError('Максимум 20 фотографий на отчёт.')
        for photo in photos:
            if photo['type'] != 'image':
                raise ValidationError('Здесь принимаются фотографии PNG/JPEG. Остальные документы добавьте в ЛК.')
            data = validate_upload(download_photo(photo['url']))
            total += data['size']
            if total > 40 * 1024 * 1024:
                raise ValidationError('Общий размер фотографий — до 40 МБ.')
            key = store_file(data)
            stored_keys.append(key)
            Photo.objects.create(draft=draft, storage_key=key, **{k: data[k] for k in ['name', 'sha256', 'size']})
        prompt(event, binding, draft)
    elif text == '/удалить_фото':
        if draft.state != 'photos':
            raise ValidationError('Для изменения фотографий сначала исправьте черновик.')
        keys = list(draft.photos.values_list('storage_key', flat=True))
        draft.photos.all().delete()
        transaction.on_commit(lambda: [private_path(key).unlink(missing_ok=True) for key in keys])
        prompt(event, binding, draft)
    elif draft.state == 'editing':
        form = form_for(draft, user)
        field_name = draft.fields[draft.step]
        field = form.fields[field_name]
        value = text
        if action in ('answer', 'default', 'skip'):
            if int(parts[2]) != draft.step:
                raise ValidationError('Это кнопка от предыдущего вопроса. Ответьте на текущий вопрос.')
            value = ':'.join(parts[3:]) if action == 'answer' else '' if action == 'skip' else str(draft.raw.get(field_name, form.initial.get(field_name, field.initial)))
        if isinstance(field, forms.DateField):
            parsed = report_date(value)
            if parsed > timezone.localdate():
                raise ValidationError('Фактическая дата не может быть в будущем.')
            value = parsed.isoformat()
        if field_name == 'note' and not value.strip():
            raise ValidationError('Уточните выбранную причину невыполнения работы.')
        field.clean(value)
        draft.raw[field_name] = value
        if field_name == 'problem':
            draft.fields = fields_for(form_for(draft, user))
            if 'note' not in draft.fields:
                draft.raw.pop('note', None)
        draft.step += 1
        draft.save(update_fields=['raw', 'step', 'fields'])
        prompt(event, binding, draft)
    else:
        queue(event, 'Используйте кнопки под текущим вопросом. Отмена: /отмена. Список работ: /работы', binding=binding, job=draft.job)
