"""Mechanic menu uses existing cabinet grants; it never inherits leader functions."""
from django.core.exceptions import PermissionDenied, ValidationError
from accounts.permissions import permitted
from accounts.models import AuditEvent
from projects.equipment import visible_equipment
from projects.equipment_issues import visible as issues
from .leadership import access_signature, link


def supported(user):
    return user.is_active and user.role=='mechanic' and not user.is_leader


def subject(user,event):
    if not supported(user) or not permitted(user,'equipment.manage'):return None
    try:
        if event.action=='equipment_updated' and event.details.get('values',{}).get('status')=='repair':
            item=visible_equipment(user).filter(pk=event.details.get('equipment'),status='repair').first()
            if item and str(item.version)==str(event.details.get('version')):
                return 'Техника требует ремонта\n'+str(item),link('Открыть технику','equipment_detail',[item.pk])
        if event.action=='equipment_issue':
            issue=issues(user).filter(pk=event.details.get('issue'),state='open').first()
            if issue:
                return 'Заявка на ремонт техники\n'+str(issue.equipment)+'\n'+issue.placement.project_object.name+'\n'+issue.description[:700],link('Открыть заявку','equipment_detail',[issue.equipment_id])
    except (ValueError,TypeError,ValidationError):return None
    return None


def delivery_allowed(user,scope):
    if not supported(user):return False
    if 'access_signature' in scope and scope['access_signature']!=access_signature(user):return False
    if scope.get('mechanic_event'):
        event=AuditEvent.objects.filter(pk=scope['mechanic_event']).first()
        return bool(event and subject(user,event))
    return True


def handle(event,binding):
    from .dialog import button,queue
    user=binding.user
    if not supported(user):raise PermissionDenied
    text=event.payload.get('text','').strip()
    category,page='menu',0
    if text.startswith('mechanic:'):
        try:
            _,category,raw_page=text.split(':');page=int(raw_page)
        except (ValueError,TypeError):raise ValidationError('Откройте меню заново: /menu')
        if category not in ('menu','equipment','repair','issues','parts') or not 0<=page<=10000:
            raise ValidationError('Откройте меню заново: /menu')
    buttons=[]
    if category=='menu':
        body='Меню механика. Выберите действие.'
        from repairs.access import can_prepare
        if can_prepare(user):buttons.append(button('Метод после ДДК','ddk:list:0'))
        for title,code in [('Техника','equipment'),('Техника в ремонте','repair'),('Заявки на ремонт','issues'),('Запчасти для ТО','parts')]:
            buttons.append(button(title,f'mechanic:{code}:0'))
        if permitted(user,'equipment.manage'):buttons.append(link('Добавить технику','equipment_new'))
        else:body+='\nДля добавления техники и запчастей руководитель должен выдать право управления техникой.'
    else:
        rows=visible_equipment(user).exclude(status='archived')
        if category=='repair':rows=rows.filter(status='repair')
        if category=='parts':rows=rows.filter(maintenance_parts__archived=False).distinct()
        if category=='issues':rows=issues(user).filter(state='open')
        rows=list(rows.order_by('pk')[page*5:page*5+6])
        body={'equipment':'Техника','repair':'Техника в ремонте','issues':'Заявки на ремонт','parts':'Запчасти для ТО'}[category]
        if not rows:body+='\nДоступных записей нет.'
        for index,row in enumerate(rows[:5],1):
            machine=row.equipment if category=='issues' else row
            body+=f'\n\n{index}. {machine}'
            if category=='issues':body+='\n'+row.placement.project_object.name+'\n'+row.description[:250]
            target=link(f'{index}. '+('Запчасти для ТО' if category=='parts' else 'Открыть карточку'),'equipment_detail',[machine.pk])
            if category=='parts':target['url']+='#maintenance-parts'
            buttons.append(target)
        if page:buttons.append(button('Назад',f'mechanic:{category}:{page-1}'))
        if len(rows)>5:buttons.append(button('Далее',f'mechanic:{category}:{page+1}'))
        buttons.append(button('Меню механика','/menu'))
    queue(event,body,buttons,binding=binding,scope={'mechanic':True,'access_signature':access_signature(user)})
