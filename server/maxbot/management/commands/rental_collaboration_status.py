from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection,transaction
from django.utils import timezone
from projects.models import RentalAccess,RentalDispute,COMPANY_TAX_ID
from maxbot.models import DemandWatch
from maxbot.rental_reminders import OMSK

class Command(BaseCommand):
    help='Read-only availability checks for rental collaboration. Does not queue messages.'
    def handle(self,*args,**options):
        with transaction.atomic():
            if connection.vendor=='postgresql':
                with connection.cursor() as cursor:cursor.execute('SET TRANSACTION READ ONLY')
            accesses=RentalAccess.objects.filter(rental__placement__equipment__legal_entity__tax_id=COMPANY_TAX_ID)
            self.stdout.write('Только чтение. Дата/время Омск: '+timezone.now().astimezone(OMSK).strftime('%d.%m.%Y %H:%M'))
            self.stdout.write(f'MAX включён: {settings.ARMADA_MAX_ENABLED}. Напоминание мастеру: 20:00; пропуск руководителю: 21:00.')
            self.stdout.write(f'Активных доступов арендодателей: {accesses.filter(active=True,user__is_active=True,user__role="lessor").count()}. Финансовых доступов: {accesses.filter(active=True,financial=True,user__role="lessor").count()}.')
            self.stdout.write(f'Открытых разногласий: {RentalDispute.objects.filter(access__in=accesses,resolved_at__isnull=True).count()}.')
            self.stdout.write(f'Наблюдателей потребности в новом формате: {DemandWatch.objects.filter(format_version=2).count()}.')
            self.stdout.write('Отправка и прочтение сообщений этим выводом не подтверждаются. Доступ арендодателю выдаёт руководитель отдельно в карточке аренды.')
