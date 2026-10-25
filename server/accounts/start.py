"""Role sets priorities; existing access services continue to set visibility."""
from django.urls import reverse
from projects.access import visible_objects,visible_contracts
from projects.equipment import visible_equipment,current,placements,due_documents as equipment_due
from projects.stock import warehouses
from projects.subcontracts import orders,can_report
from projects.closure import pending_contracts
from documents.access import visible_documents,unsorted_documents
from documents.deadlines import due_documents
from repairs.access import visible_jobs,reportable_jobs

TITLES={
 'leader':('Проверки и сводка объектов','Результаты работ, заявки на закрытие и состояние объектов'),
 'foreman':('Мои задания','Назначенные вам ремонты, результаты и возвраты на доработку'),
 'technician':('Ведомости и назначения','Подготовка работ внутри контрактов и назначения бригад'),
 'mechanic':('Техника на объектах','Доступная техника, текущие назначения и сроки документов'),
 'safety':('Документы и сроки — ОТ и ПБ','Удостоверения, медосмотры и допуски в пределах доступа к файлам'),
 'pto':('Работа ПТО','Контракты, рабочие документы и исполнительная документация'),
 'as_built':('Исполнительная документация','Доступные комплекты, файлы и документы на разбор'),
 'supply':('Склады и материалы','Остатки и операции по доступным складам'),
 'accounting':('Финансы и документы','Финансовый учёт и доступные документы'),
 'subcontractor':('Мои поручения','Работы, которые вам поручены, и их результаты'),
 'ecology':('Объекты и документы — экология','Доступные объекты и рабочие документы'),
 'other':('Моя работа','Доступные разделы и задачи'),
}

def role_home(user):
    if not user.is_authenticated or not user.is_active:return None
    role='leader' if user.is_leader else user.role if user.role in TITLES else 'other'
    title,description=TITLES[role]
    grants=set(user.grants.values_list('permission',flat=True))
    def can(key):return user.is_leader or key in grants
    cards=[];rows=[];due=[];equipment_dates=[];locations=[]
    objects=visible_objects(user).exclude(status='archived')
    contracts=visible_contracts(user).exclude(status='archived')
    docs=visible_documents(user).filter(archived=False)
    jobs=visible_jobs(user).exclude(contract__status='archived')
    def card(label,name,count=None,query='',detail=''):
        cards.append({'title':label,'url':reverse(name)+query,'count':count,'detail':detail})
    def work():
        if contracts.exists() or can('projects.manage') or can('repairs.prepare'):
            card('Контракты и работы','work_home',contracts.count(),detail='Выберите контракт и место выполнения')
    if role=='leader':
        from projects.models import ContractChange
        card('Указания заказчика на рассмотрении','contract_changes_review',ContractChange.objects.filter(contract__in=contracts,state='review').count())
        card('Результаты ремонтов на проверке','repairs',jobs.filter(state='reported').count(),'?state=reported','Посмотреть результат, подтвердить или вернуть на доработку')
        card('Заявки на закрытие контрактов','contracts',pending_contracts(user).count())
        card('Результаты субподрядчиков','subcontracts',orders(user).filter(state='reported').exclude(contract__status='archived').count(),detail='Проверка выполнения порученных работ')
        card('Действующие объекты','objects',objects.count())
        if not objects.exists():
            card('Создать объект','object_new',detail='Затем добавьте контракт и укажите связанные объекты')
        rows=[{'title':j.defect_status+' · '+', '.join('км '+d.km+' · № '+d.number for d in j.active_defects),'detail':j.section.name,'url':reverse('repair_detail',args=[j.pk]),'action':'Проверить результат','tone':j.status_tone} for j in jobs.filter(state='reported').order_by('pk')[:5]]
    elif role=='foreman':
        card('Черновики на этом устройстве','repair_drafts',detail='Неотправленные сведения и фотографии')
        own=jobs.filter(assigned_to=user)
        todo=own.exclude(state__in=['confirmed','reported'])
        card('Все назначенные мне работы','repairs',own.count(),'?master='+str(user.pk))
        card('Мои результаты на проверке','repairs',own.filter(state='reported').count(),'?master='+str(user.pk)+'&state=reported')
        card('Вернули мне на доработку','repairs',own.exclude(review_note='').count(),'?master='+str(user.pk)+'&state=revision')
        selected=list(todo.order_by('planned_date','pk')[:8])
        allowed=set(reportable_jobs(user).filter(pk__in=[j.pk for j in selected]).values_list('pk',flat=True))
        for job in selected:
            rows.append({'title':', '.join('км '+d.km+' · № '+d.number for d in job.active_defects),'detail':job.section.name+' · '+job.get_method_display()+' · '+(job.review_note or job.reason_display or job.defect_status),'url':reverse('repair_report',args=[job.pk,'repaired']) if job.pk in allowed else reverse('repair_detail',args=[job.pk]),'action':'Внести результат' if job.pk in allowed else 'Открыть задание','tone':job.status_tone})
    elif role=='technician':
        work()
        if can('repairs.prepare'):card('Подготовить ведомость','work_home',detail='Откройте контракт и участок для загрузки Excel')
        if can('personnel.manage'):card('Бригады и назначения','brigades')
        if can('projects.access'):card('Доступ к объектам и контрактам','objects',detail='Выдача доступа в карточке выбранного объекта или контракта')
        if can('users.manage'):card('Учётные записи и права','users')
        card('Документы на разбор','documents',unsorted_documents(user).count(),'?mode=unsorted')
    elif role=='mechanic':
        equipment=visible_equipment(user).exclude(status='archived')
        if can('equipment.manage') or equipment.exists():
            card('Доступная техника','equipment',equipment.count())
            card('Техника в ремонте','equipment',equipment.filter(status='repair').count(),'?state=repair')
        if can('equipment.manage'):card('Добавить технику','equipment_new')
        locations=list(current(placements(user)).exclude(equipment__status='archived').order_by('equipment__name','pk')[:8])
        equipment_dates=equipment_due(user)
    elif role=='safety':
        due=due_documents(user)
        card('Сроки требуют внимания','document_deadlines',len(due))
        card('Документы персонала','documents',docs.filter(section='personnel').count(),'?section=personnel')
        if can('document.upload'):card('Загрузить документы','document_upload',query='?section=personnel')
        equipment_dates=equipment_due(user)
    elif role=='supply':
        stock=warehouses(user).filter(archived=False)
        if can('stock.manage') or stock.exists():card('Доступные склады и остатки','stock',stock.count())
        if can('stock.manage'):card('Создать склад','stock_new')
        work()
    elif role=='subcontractor':
        own=orders(user).exclude(contract__status='archived')
        card('Мои поручения','subcontracts',own.count())
        for order in own.order_by('pk')[:8]:
            rows.append({'title':order.title,'detail':order.project_object.name+' · '+order.work_status,'url':reverse('subcontract_order',args=[order.pk]),'action':'Внести выполнение' if can_report(user,order) else 'Открыть поручение','tone':''})
    elif role=='accounting':
        if can('finance.read') or can('finance.write'):card('Финансовый учёт','finance')
        card('Доступные акты и сметы','documents',docs.filter(section='acts').count(),'?section=acts')
    elif role in ('pto','as_built'):
        work()
        card('Исполнительная документация','documents',docs.filter(section='as_built').count(),'?section=as_built')
        card('Документы на разбор','documents',unsorted_documents(user).count(),'?mode=unsorted')
        if can('document.upload'):card('Загрузить документацию','document_upload')
    else:
        work()
        card('Доступные объекты','objects',objects.count())
        card('Доступные документы','documents',docs.count())
    # Delegated powers remain reachable even when the employee's primary role is different.
    extras=[]
    for permission,label,name in [('repairs.prepare','Подготовка работ','work_home'),('personnel.manage','Бригады и назначения','brigades'),('equipment.manage','Управление техникой','equipment'),('stock.manage','Управление складами','stock'),('subcontract.manage','Субподрядчики','subcontracts'),('users.manage','Учётные записи и права','users'),('projects.access','Объекты и доступ','objects')]:
        if can(permission) and reverse(name) not in {c['url'].split('?')[0] for c in cards}:
            extras.append({'title':label,'url':reverse(name)})
    return {'role':role,'title':title,'description':description,'cards':cards,'rows':rows,'due':due[:8],'due_count':len(due),'equipment_due':equipment_dates[:8],'equipment_due_count':len(equipment_dates),'locations':locations,'extras':extras,'common_collapsed':not user.is_leader}
