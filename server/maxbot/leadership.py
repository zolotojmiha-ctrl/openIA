"""Scoped read-only management menu; decisions stay in the cabinet."""
import hashlib
from uuid import UUID
from urllib.parse import urlencode
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone
from accounts.models import AuditEvent
from accounts.permissions import permitted
from projects.access import visible_objects, visible_contracts, contract_card
from projects.equipment import visible_equipment, placements
from projects.models import MaterialRequest
from repairs.access import visible_jobs, visible_sections


def supported(user):
    return user.is_active and (user.is_leader or user.role == 'pto')


def link(text, route, args=None, query=None):
    url = settings.ARMADA_MAX_PUBLIC_ORIGIN.rstrip('/') + reverse(route, args=args)
    if query:url += '?' + urlencode(query)
    return {'type': 'link', 'text': text[:100], 'url': url}


def contract_name(user, contract):
    return contract_card(contract,user)['title'] if contract else 'Без контракта'


def title(user,job):
    from .dialog import job_title
    return 'Контракт: ' + contract_name(user,job.contract) + '\n' + job_title(job)


def materials(user):
    return MaterialRequest.objects.filter(project_object__in=visible_objects(user),
        contract__in=visible_contracts(user).exclude(status='archived'),
        status__in=['needed', 'ordered']).select_related('project_object', 'contract')


def rentals(user):
    from projects import rentals as service
    rows=service.visible(user)
    if not service.financial_reader(user):return rows.none()
    # The rental ledger grants can be wider than object grants. MAX is scoped to both.
    return rows.filter(placement__project_object__in=visible_objects(user),placement__contract__in=visible_contracts(user).exclude(status='archived'))


def validate_scope(user,scope):
    result={}
    if scope.get('contract'):
        contract=visible_contracts(user).exclude(status='archived').filter(pk=scope['contract']).first()
        if not contract:raise PermissionDenied
        result['contract']=str(contract.pk)
    if scope.get('section'):
        rows=visible_sections(user).exclude(project_object__status='archived')
        if result.get('contract'):rows=rows.filter(project_object__contracts=contract)
        section=rows.filter(pk=scope['section']).first()
        if not section:raise PermissionDenied
        result['section']=str(section.pk)
    return result


def access_signature(user):
    data=[str(user.role),str(user.is_leader),
        sorted(str(pk) for pk in visible_objects(user).values_list('pk',flat=True)),
        sorted((str(c.pk),contract_name(user,c)) for c in visible_contracts(user)),
        [permitted(user,p) for p in ('repairs.prepare','stock.manage','rental.manage','finance.read','finance.write','equipment.manage')]]
    return hashlib.sha256(repr(data).encode()).hexdigest()


def subject(user, event):
    details = event.details
    try:
        if event.action=='repair_reported' and details.get('kind')=='note':
            job=visible_jobs(user).exclude(contract__status='archived').exclude(section__project_object__status='archived').filter(pk=details.get('entity_id')).first()
            if not job:return None
            report=job.reports.filter(pk=details.get('report_id'),context__progress__isnull=False).first()
            if not report:return None
            return 'Фото хода работ · '+report.performed_date.strftime('%d.%m.%Y')+'\n'+title(user,job),link('Открыть ход работ','repair_detail',[job.pk])
        if event.action == 'repair_reported' and details.get('kind') in ('complete', 'partial', 'problem', 'correction'):
            job = visible_jobs(user).exclude(contract__status='archived').filter(pk=details.get('entity_id')).first()
            if not job or (details.get('kind') != 'problem' and job.state != 'reported'):return None
            latest = job.reports.order_by('-created_at').first()
            if not latest or str(latest.pk) != details.get('report_id'):return None
            heading = 'Препятствие при ремонте' if details.get('kind') == 'problem' else 'Отчёт по ремонту на проверке'
            return heading+'\n'+title(user,job), link('Открыть отчёт в ЛК', 'repair_detail', [job.pk])
        if event.action == 'equipment_updated' and details.get('values', {}).get('status') == 'repair':
            candidates=visible_equipment(user).filter(pk=details.get('equipment'),status='repair')
            if not user.is_leader:candidates=candidates.filter(pk__in=placements(user).filter(contract__in=visible_contracts(user)).values('equipment_id'))
            item=candidates.first()
            if item and str(item.version) == str(details.get('version')):
                locations=placements(user).filter(equipment=item).select_related('contract','project_object').order_by('-start_date')[:3]
                context=''.join('\nКонтракт: '+contract_name(user,p.contract)+'\n'+p.project_object.name for p in locations)
                return 'Техника требует ремонта\n'+str(item)+context,link('Открыть технику в ЛК','equipment_detail',[item.pk])
        if event.action=='equipment_issue':
            from projects.equipment_issues import visible
            issue=visible(user).filter(pk=details.get('issue'),state='open').first()
            if issue:
                return ('Заявка на ремонт техники\nКонтракт: '+contract_name(user,issue.placement.contract)+'\n'+issue.placement.project_object.name+'\n'+str(issue.equipment)+'\n'+issue.description[:700]),link('Открыть заявку в ЛК','equipment_detail',[issue.equipment_id])
        if event.action == 'repair_prepared' and details.get('kind') == 'material_request':
            item = materials(user).filter(pk=details.get('record')).first()
            latest = AuditEvent.objects.filter(action='repair_prepared',details__kind='material_request',details__record=details.get('record')).order_by('-created_at','-pk').first()
            if item and latest and latest.pk == event.pk:
                return ('Потребность в материалах\nКонтракт: '+contract_name(user,item.contract)+'\n'+item.project_object.name+'\n'+f'{item.name}: {item.quantity} {item.unit}'),link('Открыть потребность в ЛК','material_edit',[item.contract_id,item.project_object_id,item.pk])
    except (ValueError, ValidationError, TypeError):return None
    return None


def delivery_allowed(user,scope):
    if not supported(user):return False
    if scope.get('access_signature')!=None and scope['access_signature']!=access_signature(user):return False
    if scope.get('demand_digest'):
        if not permitted(user,'stock.manage'):return False
        from .demand_watch import snapshot
        try:current_digest=snapshot(user)[0]
        except ValidationError:return False
        if current_digest!=scope['demand_digest']:return False
    if scope.get('leader_event'):
        event=AuditEvent.objects.filter(pk=scope['leader_event']).first()
        return bool(event and subject(user,event))
    try:validate_scope(user,scope.get('selection',{}))
    except (PermissionDenied,ValidationError,ValueError):return False
    return True


def handle(event,binding):
    from .dialog import button,queue
    from projects import rentals as rental_service
    from projects.equipment_issues import visible as issues
    from finance.templatetags.finance_format import rub
    user=binding.user
    if not supported(user):raise PermissionDenied
    text=event.payload.get('text','');category='menu';page=0
    try:selection=validate_scope(user,binding.menu_scope)
    except (PermissionDenied,ValidationError,ValueError):
        selection={};binding.menu_scope={};binding.save(update_fields=['menu_scope'])
    if text.startswith('scope:'):
        try:
            _,kind,value=text.split(':')
            if kind in ('contract','section'):
                value='' if value=='all' else str(UUID(value))
                selection=({'contract':value} if value else {}) if kind=='contract' else {**selection,'section':value}
                selection=validate_scope(user,selection)
                binding.menu_scope=selection;binding.save(update_fields=['menu_scope'])
            elif kind in ('contracts','sections'):category=kind;page=int(value)
            else:raise ValueError
        except (ValueError,TypeError):raise ValidationError('Откройте меню: /меню')
    elif text.startswith('leader:'):
        try:_,category,raw=text.split(':');page=int(raw)
        except (ValueError,TypeError):raise ValidationError('Откройте меню: /меню')
    if category not in ('menu','summary','works','progress','reports','equipment','issues','materials','shortage','rentals','contracts','sections') or not 0<=page<=10000:raise ValidationError('Неверная страница меню.')
    jobs=visible_jobs(user).exclude(contract__status='archived').exclude(section__project_object__status='archived')
    machines=visible_equipment(user);needed=materials(user);hire=rentals(user);complaints=issues(user).filter(state='open')
    query={}
    if selection.get('contract'):
        value=selection['contract'];jobs=jobs.filter(contract_id=value);needed=needed.filter(contract_id=value);hire=hire.filter(placement__contract_id=value);complaints=complaints.filter(placement__contract_id=value);query['contract']=value
    if selection.get('section'):
        section=visible_sections(user).get(pk=selection['section']);jobs=jobs.filter(section=section);needed=needed.filter(project_object=section.project_object);hire=hire.filter(placement__project_object=section.project_object);complaints=complaints.filter(placement__project_object=section.project_object);query['object']=str(section.project_object_id)
    p=placements(user)
    if selection.get('contract'):p=p.filter(contract_id=selection['contract'])
    if selection.get('section'):p=p.filter(project_object=section.project_object)
    machines=machines.filter(pk__in=p.values('equipment_id')) if selection or not user.is_leader else machines
    reports=jobs.filter(state='reported')
    contract=visible_contracts(user).filter(pk=selection.get('contract')).first() if selection.get('contract') else None
    context='Контракт: '+(contract_name(user,contract) if contract else 'Все')+'\nУчасток: '+(section.project_object.name+' · '+section.name if selection.get('section') else 'Все')
    menu=[('Работы и дефекты','works'),('Фото хода работ','progress'),('Отчёты на проверку','reports'),('Техника в ремонте','equipment'),('Заявки на ремонт техники','issues'),('Заявки на материалы','materials'),('Нехватка муфт','shortage'),('Сводка по участкам','summary')]
    if rental_service.financial_reader(user):menu.insert(-1,('Аренда: наработка и стоимость','rentals'))
    if not permitted(user,'stock.manage'):
        menu=[entry for entry in menu if entry[1]!='shortage']
        if category=='shortage':raise PermissionDenied
    body=context;buttons=[]
    if category in ('contracts','sections'):
        rows=visible_contracts(user).exclude(status='archived').order_by('pk') if category=='contracts' else visible_sections(user).exclude(project_object__status='archived').order_by('pk')
        if category=='sections' and contract:rows=rows.filter(project_object__contracts=contract)
        entries=list(rows[page*5:page*5+6]);kind='contract' if category=='contracts' else 'section'
        buttons=[button('Все контракты' if kind=='contract' else 'Все участки',f'scope:{kind}:all')]
        buttons += [button(contract_name(user,r) if kind=='contract' else r.project_object.name+' · '+r.name,f'scope:{kind}:{r.pk}') for r in entries[:5]]
        if len(entries)>5:buttons.append(button('Далее',f'scope:{category}:{page+1}'))
    elif category in ('menu','summary'):
        body=('Меню руководителя' if user.is_leader else 'Меню ПТО')+'\n'+context+f'\nОтчётов на проверке: {reports.count()}\nТехники в ремонте: {machines.filter(status="repair").count()}\nЗаявок на материалы: {needed.count()}'
        if category=='summary':
            for section_id in jobs.values_list('section_id',flat=True).distinct()[:8]:
                row=jobs.filter(section_id=section_id);sample=row.first()
                body+='\n'+sample.section.name+f': на проверке {row.filter(state="reported").count()}, подтверждено {row.filter(state="confirmed").count()}'
        buttons=[button('Выбрать контракт','scope:contracts:0'),button('Выбрать участок','scope:sections:0')]+[button(label,f'leader:{key}:0') for label,key in menu]
    elif category=='shortage':
        from projects.repair_materials import all_demand
        allowed=set(jobs.values_list('pk',flat=True));lines=[];unknown=0
        for record in all_demand(user):
            job=record['job']
            if job.pk not in allowed:continue
            unknown+=bool(record['coupling_issues'])
            for line in record['couplings']:
                if line['missing']:lines.append((job,line))
        for job,line in lines[page*5:page*5+5]:
            body+='\n\n'+title(user,job)+f'\nНе хватает: {line["missing"]} шт. · {line["length"]} мм · стенка {line["wall"]} мм'
        if not lines:body+='\nНехватка пока не рассчитана.' if unknown else '\nНехватки муфт нет.'
        if unknown:body+=f'\nРабот, требующих уточнения расчёта: {unknown}. Проверьте в ЛК.'
        if len(lines)>page*5+5:buttons.append(button('Далее',f'leader:shortage:{page+1}'))
        buttons.append(link('Подробная потребность в ЛК','stock_coverage',query=query))
    else:
        queryset={'works':jobs,'progress':jobs.filter(reports__context__progress__isnull=False).distinct(),'reports':reports,'equipment':machines.filter(status='repair'),'issues':complaints,'materials':needed,'rentals':hire}[category].order_by('pk')
        entries=list(queryset[page*5:page*5+6])
        if not entries:body+='\nЗаписей нет.'
        for index,row in enumerate(entries[:5],1):
            if category in ('works','progress','reports'):
                body+='\n\n'+title(user,row)[:500]+'\n'+row.get_state_display()
                if category=='progress':
                    latest=row.reports.filter(context__progress__isnull=False).first()
                    body+='\nФото за '+latest.performed_date.strftime('%d.%m.%Y')+f': {latest.attachments.count()} шт.'
                target=link(f'{index}. Открыть ход работ' if category=='progress' else f'{index}. Открыть работу','repair_detail',[row.pk])
                if category=='progress':target['url']+='#progress-timeline'
                buttons.append(target)
            elif category in ('equipment','issues'):
                machine=row.equipment if category=='issues' else row
                body+='\n\n'+str(machine)+('\n'+row.description[:350] if category=='issues' else '')
                buttons.append(link(f'{index}. Открыть технику','equipment_detail',[machine.pk]))
            elif category=='rentals':
                totals=rental_service.totals(row,timezone.localdate());missing=len(rental_service.missing_dates(row))
                body+='\n\n'+str(row.placement.equipment)+'\n'+row.placement.project_object.name+'\nНа '+timezone.localdate().strftime('%d.%m.%Y')+f': {totals["hours"]} оплачиваемых ч\nНачислено: {rub(totals["charged"])}\nОплачено: {rub(totals["paid"])}\nК оплате: {rub(totals["debt"])}\nАванс: {rub(totals["advance"])}'
                if missing or totals['unpriced']:body+=f'\nРасчёт неполный: дней без отметки {missing}, без тарифа {totals["unpriced"]}.'
                buttons.append(link(f'{index}. Открыть табель','rental_detail',[row.pk]))
            else:
                body+='\n\nКонтракт: '+contract_name(user,row.contract)+'\n'+row.project_object.name+f'\n{row.name}: {row.quantity} {row.unit}'
                buttons.append(link(f'{index}. Открыть заявку','material_edit',[row.contract_id,row.project_object_id,row.pk]))
        if len(entries)>5:buttons.append(button('Далее',f'leader:{category}:{page+1}'))
    if category!='menu':buttons.append(button('Меню','/меню'))
    queue(event,body,buttons,binding=binding,scope={'leader':True,'selection':selection,'access_signature':access_signature(user)})
