"""Human-facing XLSX columns over the existing checked import commands.

Record identities and versions travel in hidden columns. Related records are
selected by their accessible names; ambiguous names never pick an arbitrary ID.
Legacy, complete machine-format worksheets remain accepted unchanged.
"""
import re
import uuid
from datetime import datetime, timedelta
from django.core.exceptions import ValidationError
from django.core import signing

PREFIX = 'Служебное: '
WORDS = {
    'own':'Собственная', 'rent':'Аренда', 'subcontractor':'Субподрядчик',
    'ready':'Готова к работе', 'working':'В работе', 'repair':'В ремонте',
    'reserve':'Резерв', 'archived':'В архиве', 'material':'Материал',
    'coupling':'Муфта', 'composite':'Композит', 'sealant':'Герметик',
    'insulation':'Изоляция', 'electrode':'Электроды', 'pcs':'шт.',
    'm2':'м²', 'm':'м', 'kg':'кг', 'l':'л', 'p1':'П1', 'p2':'П2',
    'p3':'П3', 'p4':'П4', 'p5u':'П5У', 'receipt':'Поступление',
    'transfer':'Перемещение', 'issue':'Выдача бригаде', 'return':'Возврат',
    'consume':'Списание на работу', 'loss':'Списание', 'repairs':'Устранение дефектов',
    'valves':'Ремонт арматуры', 'diagnostics':'Диагностика', 'construction':'Строительство',
    'complete':'Выполнено', 'partial':'Частично выполнено', 'problem':'Есть препятствие',
    'subcontract':'Субподряд', 'equipment_rent':'Аренда техники', 'housing':'Проживание',
    'supply':'Поставка', 'other':'Другое', 'requested':'Заявка', 'active':'Действует',
    'disabled':'Отключён',
}

REFS = {
    'ID объекта':'objects', 'ID объектов (через ;)':'objects',
    'ID контракта':'contracts', 'ID контракта заказчика':'contracts',
    'ID техники':'equipment', 'ID документа':'documents', 'ID документа-основания':'documents',
    'ID мастера (персонал)':'people', 'ID сотрудника (персонал)':'people',
    'ID сотрудника':'people', 'ID бригады':'crews',
    'ID назначения бригады':'placements', 'ID компании':'companies',
    'ID поручения':'orders', 'ID ответственного':'users', 'ID пользователя':'users',
    'ID учётной записи':'users', 'ID представителя':'representatives',
    'ID откуда':'warehouses', 'ID куда':'warehouses', 'ID позиции':'items',
    'ID работы':'jobs', 'ID ремонта':'jobs', 'ID конкретной муфты':'couplings',
    'ID указания заказчика':'changes',
}
LABELS = {
    'ID объекта':'Участок работ', 'ID объектов (через ;)':'Участки работ (через ;)',
    'ID контракта':'Контракт', 'ID контракта заказчика':'Контракт заказчика',
    'ID техники':'Техника', 'ID документа':'Документ', 'ID документа-основания':'Документ-основание',
    'ID мастера (персонал)':'Мастер', 'ID сотрудника (персонал)':'Сотрудник',
    'ID сотрудника':'Сотрудник', 'ID бригады':'Бригада',
    'ID назначения бригады':'Бригада на участке', 'ID компании':'Компания',
    'ID поручения':'Поручение', 'ID ответственного':'Ответственный',
    'ID пользователя':'Пользователь', 'ID учётной записи':'Учётная запись',
    'ID представителя':'Представитель', 'ID откуда':'Со склада', 'ID куда':'На склад',
    'ID позиции':'Материал', 'ID работы':'Работа / дефект', 'ID ремонта':'Работа / дефект',
    'ID конкретной муфты':'Конкретная муфта', 'ID указания заказчика':'Указание заказчика',
}


def system(topic, headers):
    hidden = {n for n,h in enumerate(headers) if h.startswith('Версия')}
    if topic != 'subcontracts.repairs':
        hidden.add(0)
    if topic == 'stock.items':hidden.update((1,10))
    if topic == 'stock.balances':hidden.update((2,3))
    if topic in ('crews.brigades','crews.members'):hidden.add(3)
    return hidden


def label(header):
    return LABELS.get(header, re.sub(r'\s*\([^)]*\)', '', header).strip())


def choices(header):
    if header=='Этап':
        from projects.models import WORK_STAGES
        return dict(pair for stages in WORK_STAGES.values() for pair in stages)
    if '0/1' in header:return {'0':'Нет', '1':'Да'}
    match = re.search(r'\(([a-z0-9_/]+)\)', header)
    if match and '/' in match[1]:return {key:WORDS.get(key,key) for key in match[1].split('/')}
    return {}


def normal_date(value, epoch):
    if not value:return ''
    try:
        if re.fullmatch(r'\d+(?:\.0+)?', value):
            days=int(float(value))
            if not 0<days<100000:raise ValueError
            return (epoch+timedelta(days=days)).isoformat()
        for pattern in ('%Y-%m-%d','%d.%m.%Y','%d/%m/%Y','%d-%m-%Y'):
            try:return datetime.strptime(value,pattern).date().isoformat()
            except ValueError:pass
    except (ValueError,OverflowError):pass
    raise ValidationError('Укажите дату, например 18.08.2026, или обычную дату Excel.')


class References:
    def __init__(self,user):self.user=user;self.cache={}

    def records(self,kind):
        if kind in self.cache:return self.cache[kind]
        from projects.access import visible_objects,visible_contracts
        from documents.access import visible_people,visible_documents,sharing_users
        from projects.crews import visible_crews,visible_assignments
        from projects import stock,equipment,subcontracts
        from repairs.access import visible_jobs
        if kind=='objects':rows=[(o.pk,o.name,[o.name]) for o in visible_objects(self.user)]
        elif kind=='contracts':rows=[(o.pk,o.number,[o.number]) for o in visible_contracts(self.user)]
        elif kind=='people':rows=[(o.pk,o.full_name,[o.full_name]) for o in visible_people(self.user)]
        elif kind=='documents':rows=[(o.pk,o.title,[o.title]) for o in visible_documents(self.user)]
        elif kind=='users':rows=[(o.pk,f'{o.full_name} · {o.phone}',[o.full_name,o.phone]) for o in sharing_users(self.user)]
        elif kind=='equipment':rows=[(o.pk,' · '.join(x for x in [o.name,o.registration_number,o.inventory_number] if x),[o.name,o.registration_number,o.inventory_number]) for o in equipment.visible_equipment(self.user)]
        elif kind=='crews':rows=[(o.pk,o.name,[o.name]) for o in visible_crews(self.user)]
        elif kind=='placements':rows=[(o.pk,f'{o.brigade.name} · {o.project_object.name} · {o.start_date}',[o.brigade.name]) for o in visible_assignments(self.user).select_related('brigade','project_object')]
        elif kind=='warehouses':rows=[(o.pk,o.name,[o.name]) for o in stock.warehouses(self.user)]
        elif kind=='items':rows=[(o.pk,f'{o.name} · {o.sku}',[o.name,o.sku]) for o in stock.items(self.user)]
        elif kind=='companies':rows=[(o.pk,f'{o.name} · ИНН {o.tax_id}' if o.tax_id else o.name,[o.name,o.tax_id]) for o in subcontracts.companies(self.user)]
        elif kind=='orders':rows=[(o.pk,f'{o.title} · {o.contract.number} · {o.project_object.name}',[o.title]) for o in subcontracts.orders(self.user,True).select_related('contract','project_object')]
        elif kind=='jobs':
            from projects.stock_planning import job_label
            rows=[(o.pk,job_label(o),[o.repair_label]) for o in visible_jobs(self.user)]
        elif kind=='representatives':
            from projects.models import SubcontractRepresentative
            rows=[(o.pk,f'{o.user.full_name} · {o.company.name}',[o.user.full_name]) for o in SubcontractRepresentative.objects.filter(company__in=subcontracts.companies(self.user)).select_related('user','company')]
        elif kind=='couplings':
            from repairs.models import Coupling
            rows=[(o.pk,str(o),[o.serial]) for o in Coupling.objects.filter(project_object__in=visible_objects(self.user))]
        elif kind=='changes':
            from projects.models import ContractChange
            rows=[(o.pk,o.current_revision.title,[o.current_revision.title]) for o in ContractChange.objects.filter(contract__in=visible_contracts(self.user),current_revision__isnull=False).select_related('current_revision')]
        else:raise ValidationError('Неизвестный справочник.')
        self.cache[kind]=[(str(pk),str(name),[str(a) for a in aliases if a]) for pk,name,aliases in rows]
        return self.cache[kind]

    def name(self,kind,pk):
        return next((name for key,name,_ in self.records(kind) if key==str(pk)),'')

    def resolve(self,kind,value,field):
        if not value:return ''
        found={pk for pk,name,aliases in self.records(kind)
               if value.casefold() in {pk.casefold(),name.casefold(),*(a.casefold() for a in aliases)}}
        if len(found)>1:raise ValidationError(f'«{field}»: найдено несколько записей «{value}». Укажите полное название из списка ЛК.')
        if not found:raise ValidationError(f'«{field}»: запись «{value}» не найдена среди доступных. Проверьте название в ЛК.')
        return found.pop()


def workbook(user,topic,rows,headers,sheet_name='Данные'):
    from repairs.excel import workbook as write
    hidden=system(topic,headers);refs=References(user);data=[];metadata=[]
    for row in rows:
        values=list(row);links={}
        for n,h in enumerate(headers):
            if n in hidden:continue
            if h in REFS:
                values[n]='; '.join(refs.name(REFS[h],v.strip()) for v in str(values[n]).split(';') if v.strip()) if 'через ;' in h else refs.name(REFS[h],values[n])
                links[str(n)]={'value':str(row[n]),'label':values[n]}
            elif choices(h):values[n]=choices(h).get(str(values[n]),values[n])
        data.append(values)
        metadata.append(signing.dumps({'actor':str(user.pk),'topic':topic,'links':links},salt='human-excel-links',compress=True))
    order=[n for n in range(len(headers)) if n not in hidden]+sorted(hidden)
    names=[PREFIX+headers[n] if n in hidden else label(headers[n]) for n in order]
    names.append(PREFIX+'Связи')
    return write([[row[n] for n in order]+[meta] for row,meta in zip(data,metadata)],headers=names,sheet_name=sheet_name,
                 hidden_columns=range(len(order)-len(hidden),len(order)+1),
                 column_choices={n:list(choices(headers[k]).values()) for n,k in enumerate(order) if choices(headers[k])})


def normalize(user,topic,book,headers):
    cells=book['cells'];incoming={str(v).strip():c for (r,c),v in cells.items() if r==1 and str(v).strip()}
    full=[str(cells.get((1,n+1),'')).strip() for n in range(len(headers))]
    if full==headers:return book  # old files keep their exact validation rules
    metadata_column=incoming.pop(PREFIX+'Связи',None)
    hidden=system(topic,headers);known={}
    for n,h in enumerate(headers):
        for key in (h,PREFIX+h,label(h) if n not in hidden else PREFIX+h):known.setdefault(key,n)
    unknown=set(incoming)-set(known)
    if unknown:raise ValidationError('Неизвестные столбцы: '+', '.join(sorted(unknown))+'. Используйте шаблон этого раздела.')
    if not incoming:raise ValidationError('В первой строке нужны названия столбцов.')
    refs=References(user);result={(1,n+1):h for n,h in enumerate(headers)}
    for row in sorted({r for r,c in cells if r>1}):
        values=['']*len(headers)
        for name,column in incoming.items():values[known[name]]=str(cells.get((row,column),'')).strip()
        if not any(values):continue
        try:
            links={}
            token=cells.get((row,metadata_column),'') if metadata_column else ''
            if token:
                try:metadata=signing.loads(token,salt='human-excel-links')
                except signing.BadSignature:raise ValidationError('Служебные связи повреждены. Скачайте текущий список и перенесите изменения в него.')
                if metadata.get('actor')!=str(user.pk) or metadata.get('topic')!=topic:raise ValidationError('Скачайте файл под своей учётной записью из этого раздела.')
                links=metadata['links']
            for n,h in enumerate(headers):
                if n in hidden:continue
                value=values[n]
                if h in REFS:
                    original=links.get(str(n))
                    if original and value==original['label']:value=original['value']
                    values[n]=';'.join(refs.resolve(REFS[h],v.strip(),label(h)) for v in value.split(';') if v.strip()) if 'через ;' in h else refs.resolve(REFS[h],value,label(h))
                elif choices(h):
                    option=choices(h);inverse={v.casefold():k for k,v in option.items()}
                    if not value:
                        if '0/1' in h:value='1' if any(w in h for w in ('Действует','Показывать','Напоминать','Просмотр')) else '0'
                        else:value={'Принадлежность':'own','Состояние':'ready' if topic.startswith('equipment') else 'requested','Результат':'complete'}.get(label(h),'')
                    values[n]=inverse.get(value.casefold(),value)
                elif value and any(w in h for w in ('Дата','Начало','Окончание','Получено','Применять с','Действует до','Срок (')):
                    values[n]=normal_date(value,book['epoch'])
            if topic=='stock.items' and not values[0] and not values[1]:values[1]='МТР-'+uuid.uuid4().hex[:12]
            if topic=='stock.items' and not values[4]:values[4]='m2' if values[3]=='insulation' else 'kg' if values[3]=='electrode' else 'pcs'
            if topic=='subcontracts.repairs' and values[0] and not values[2]:
                from repairs.access import visible_jobs
                values[2]=str(visible_jobs(user).get(pk=values[0]).version)
            if topic=='documents.deadlines' and not values[0] and values[1]:
                from documents.access import visible_documents
                values[0]=refs.resolve('documents',values[1],'Документ')
                values[-1]=str(visible_documents(user).get(pk=values[0]).revision)
            if topic=='equipment.documents' and not values[6]:values[6]='30'
            if topic=='documents.deadlines' and not values[4]:values[4]='30'
            if topic in ('registry.objects','registry.contracts') and not values[4 if topic.endswith('objects') else 7]:
                values[4 if topic.endswith('objects') else 7]='planned'
            for n,value in enumerate(values):result[(row,n+1)]=value
        except ValidationError as exc:raise ValidationError(f'Строка {row}: '+'; '.join(exc.messages)) from exc
    return {**book,'cells':result}
