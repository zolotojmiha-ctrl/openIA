"""Read-only leader menu. Decisions remain in the authenticated cabinet."""
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from accounts.models import AuditEvent
from projects.access import visible_objects, visible_contracts
from projects.equipment import visible_equipment
from projects.models import MaterialRequest
from repairs.access import visible_jobs


def link(text, route, args=None):
    return {'type': 'link', 'text': text[:100],
            'url': settings.ARMADA_MAX_PUBLIC_ORIGIN.rstrip('/') + reverse(route, args=args)}


def materials(user):
    return MaterialRequest.objects.filter(project_object__in=visible_objects(user),
        contract__in=visible_contracts(user).exclude(status='archived'),
        status__in=['needed', 'ordered']).select_related('project_object', 'contract')


def subject(user, event):
    """Re-evaluate a notice against current state at creation AND delivery."""
    details = event.details
    try:
        if event.action == 'repair_reported' and details.get('kind') in ('complete', 'partial', 'problem', 'correction'):
            job = visible_jobs(user).exclude(contract__status='archived').filter(pk=details.get('entity_id')).first()
            if not job or (details.get('kind') != 'problem' and job.state != 'reported'):
                return None
            latest = job.reports.order_by('-created_at').first()
            if not latest or str(latest.pk) != details.get('report_id'):
                return None
            title = 'Препятствие при ремонте' if details.get('kind') == 'problem' else 'Отчёт по ремонту на проверке'
            return title + '\n' + job.section.project_object.name + ' · ' + job.section.name, link('Открыть отчёт в ЛК', 'repair_detail', [job.pk])
        if event.action == 'equipment_updated' and details.get('values', {}).get('status') == 'repair':
            item = visible_equipment(user).filter(pk=details.get('equipment'), status='repair').first()
            if item and str(item.version) == str(details.get('version')):
                return 'Техника требует ремонта\n' + str(item), link('Открыть технику в ЛК', 'equipment_detail', [item.pk])
        if event.action == 'repair_prepared' and details.get('kind') == 'material_request':
            item = materials(user).filter(pk=details.get('record')).first()
            latest = AuditEvent.objects.filter(action='repair_prepared', details__kind='material_request',
                details__record=details.get('record')).order_by('-created_at', '-pk').first()
            if item and latest and latest.pk == event.pk:
                return ('Потребность в материалах\n' + item.project_object.name + '\n' +
                        f'{item.name}: {item.quantity} {item.unit}'), link('Открыть потребность в ЛК', 'material_edit',
                            [item.contract_id, item.project_object_id, item.pk])
    except (ValueError, ValidationError, TypeError):
        return None
    return None


def delivery_allowed(user, scope):
    if not user.is_leader:
        return False
    if scope.get('leader_event'):
        event = AuditEvent.objects.filter(pk=scope['leader_event']).first()
        return bool(event and subject(user, event))
    return True


def handle(event, binding):
    from .dialog import button, queue, job_title
    user = binding.user
    if not user.is_leader:
        raise PermissionDenied
    text = event.payload.get('text', '')
    category, page = 'menu', 0
    if text.startswith('leader:'):
        try:
            _, category, raw_page = text.split(':')
            page = int(raw_page)
        except (ValueError, TypeError):
            raise ValidationError('Откройте меню руководителя: /меню')
        if category not in ('menu', 'reports', 'equipment', 'materials', 'summary') or not 0 <= page <= 10000:
            raise ValidationError('Неверная страница меню.')
    jobs = visible_jobs(user).exclude(contract__status='archived')
    reports = jobs.filter(state='reported').order_by('pk')
    equipment = visible_equipment(user).filter(status='repair').order_by('pk')
    needed = materials(user).order_by('pk')
    menus = [('Отчёты на проверку', 'reports'), ('Техника и ремонты', 'equipment'),
             ('Потребность в материалах', 'materials'), ('Сводка по участкам', 'summary')]
    buttons = []
    if category in ('menu', 'summary'):
        body = ('Меню руководителя' if category == 'menu' else 'Сводка по доступным участкам')
        body += f'\nОтчётов на проверке: {reports.count()}\nТехники в ремонте: {equipment.count()}\nЗаявок на материалы: {needed.count()}'
        if category == 'summary':
            for section in jobs.values_list('section_id', 'section__name').distinct().order_by('section__name', 'section_id')[:8]:
                rows = jobs.filter(section_id=section[0])
                body += f'\n{section[1][:100]}: на проверке {rows.filter(state="reported").count()}, подтверждено {rows.filter(state="confirmed").count()}'
            body += '\nПолный список участков доступен в ЛК.'
            buttons.append(link('Открыть работы в ЛК', 'repairs'))
        buttons += [button(title, f'leader:{key}:0') for title, key in menus]
    else:
        queryset = {'reports': reports, 'equipment': equipment, 'materials': needed}[category]
        rows = list(queryset[page * 5:page * 5 + 6])
        body = dict((key, title) for title, key in menus)[category]
        if not rows:
            body += '\nЗаписей нет.'
        for index, row in enumerate(rows[:5], 1):
            if category == 'reports':
                body += '\n\n' + job_title(row)[:450]
                buttons.append(link(f'{index}. Открыть отчёт', 'repair_detail', [row.pk]))
            elif category == 'equipment':
                body += '\n\n' + str(row)[:200] + '\n' + row.notes[:200]
                buttons.append(link(f'{index}. Открыть технику', 'equipment_detail', [row.pk]))
            else:
                body += f'\n\n{row.project_object.name[:100]}\n{row.name[:200]}: {row.quantity} {row.unit[:30]}\n{row.get_status_display()}'
                buttons.append(link(f'{index}. Открыть потребность', 'material_edit', [row.contract_id, row.project_object_id, row.pk]))
        if len(rows) > 5:
            buttons.append(button('Следующая страница', f'leader:{category}:{page + 1}'))
        if page:
            buttons.append(button('В начало списка', f'leader:{category}:0'))
        if category == 'materials':
            buttons.append(link('Общая потребность и обеспеченность', 'stock_coverage'))
        buttons.append(button('Меню руководителя', '/меню'))
    queue(event, body, buttons, binding=binding, scope={'leader': True})
