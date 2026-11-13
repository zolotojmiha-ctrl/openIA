from django.core.management.base import BaseCommand
from django.core.exceptions import ValidationError
from postoffice.models import Mailbox
from postoffice import transport
from postoffice.trash import capabilities


class Command(BaseCommand):
    help='Проверить поддержку корзины и адресной очистки, не перемещая и не удаляя письма.'
    def handle(self,*args,**options):
        for box in Mailbox.objects.filter(state='active'):
            try:
                with transport.connect(box) as client:
                    trash=any(f.get('trash') for f in transport.folders(client))
                    purge='UIDPLUS' in capabilities(client)
                    move='MOVE' in capabilities(client) or purge
                self.stdout.write(f'{box.address}: корзина {"есть" if trash else "не найдена"}; перемещение {"доступно" if move else "недоступно"}; очистка через 30 дней {"поддерживается" if purge and trash else "требует проверки сервера"}.')
            except ValidationError:self.stdout.write(f'{box.address}: соединение не подтверждено; реквизиты не выводятся.')
