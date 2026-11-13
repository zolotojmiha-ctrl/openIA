"""Read-only diagnostics; never queues messages or fills rental days."""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection,transaction
from django.utils import timezone
from accounts.models import User
from projects.models import EquipmentRental,COMPANY_TAX_ID
from projects import rentals
from maxbot.models import Binding,Outbox
from maxbot.links import current
from maxbot.rental_reminders import OMSK,HOUR,key,pending

class Command(BaseCommand):
    help='Read rental reminder recipients, responsible assignments and MAX link state. No writes.'
    def add_arguments(self,parser):parser.add_argument('--name',help='Часть ФИО сотрудника')
    def handle(self,*args,**options):
        with transaction.atomic():
            if connection.vendor=='postgresql':
                with connection.cursor() as cursor:cursor.execute('SET TRANSACTION READ ONLY')
            day=timezone.now().astimezone(OMSK).date()
            self.stdout.write(f'Только чтение. Дата: {day:%d.%m.%Y}. Напоминание: {HOUR}:00, Омск. MAX включён: {settings.ARMADA_MAX_ENABLED}.')
            users=User.objects.all().order_by('full_name')
            users=users.filter(full_name__icontains=options['name']) if options.get('name') else users.filter(role='foreman',is_active=True)
            count=0
            for user in users:
                count+=1;binding=Binding.objects.filter(user=user,active=True).select_related('user').first()
                valid=bool(binding and current(binding))
                self.stdout.write(f'\n{user.full_name}: роль {user.role}; активен {user.is_active}; актуальный MAX {valid}.')
                rows=EquipmentRental.objects.filter(placement__equipment__legal_entity__tax_id=COMPANY_TAX_ID,placement__start_date__lte=day).select_related('placement__equipment','placement__project_object','placement__contract','placement__crew_assignment__master')
                rows=rows.filter(placement__end_date__isnull=True)|rows.filter(placement__end_date__gte=day)
                for rental in rows.order_by('pk'):
                    p=rental.placement;machine=p.equipment;entry=rental.days.filter(date=day).first()
                    allowed=rentals.allowed_on(user,rental,day);due=pending(user,rental,day)
                    message=Outbox.objects.filter(key=key(user,rental,day)).values_list('state',flat=True).first() or 'не создано'
                    master=p.crew_assignment.master.full_name if p.crew_assignment_id else 'не указан; проверяется закрепление за бригадой'
                    self.stdout.write(f'  {machine.name} · {machine.registration_number}: {p.project_object.name}; мастер назначения: {master}.')
                    self.stdout.write(f'    Может отметить сегодня: {allowed}; отметка: {entry.get_kind_display() if entry else "нет"}; ожидает напоминания: {due and valid}; очередь: {message}.')
            if not count:self.stdout.write('Сотрудники не найдены.')
