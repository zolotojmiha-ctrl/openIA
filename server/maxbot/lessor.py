"""External owner menu; callbacks only read explicitly granted rental ledgers."""
from django.core.exceptions import PermissionDenied,ValidationError
from django.db.models import Sum
from projects import rental_owner
from .labels import location


def delivery_allowed(user,scope):
    rows=rental_owner.visible(user)
    if scope.get('rental'):return rows.filter(rental_id=scope['rental']).exists()
    return user.is_active and user.role=='lessor'


def handle(event,binding):
    from .dialog import queue,button
    from .leadership import link
    text=event.payload.get('text','');page=0
    if text.startswith('lessor:'):
        try:page=int(text.split(':')[1])
        except (ValueError,IndexError):raise ValidationError('Откройте меню заново.')
        if not 0<=page<=10000:raise ValidationError('Откройте меню заново.')
    rows=list(rental_owner.visible(binding.user).order_by('pk')[page*5:page*5+6])
    body='Моя техника в аренде';buttons=[]
    for access in rows[:5]:
        rental=access.rental;p=rental.placement
        hours=rental.days.aggregate(total=Sum('hours'))['total'] or 0
        body+=f'\n\n{p.equipment}\n{p.project_object.name}\nПо табелю: {hours} ч'
        buttons.append(link('Табель: '+p.equipment.name,'lessor_detail',[rental.pk]))
    if not rows:body+='\nАренды пока не предоставлены. Обратитесь к руководителю.'
    if len(rows)>5:buttons.append(button('Далее',f'lessor:{page+1}'))
    buttons.append(link('Открыть мою технику','lessor_home'))
    queue(event,body[:3500],buttons,binding=binding,scope={'lessor':True})
