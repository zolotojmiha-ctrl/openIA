from django.core.management.base import BaseCommand,CommandError
from django.core.exceptions import ValidationError
from postoffice.models import Mailbox
from postoffice import transport
from postoffice.trash import capabilities


class Command(BaseCommand):
    help='Проверить поддержку корзины и адресной очистки, не перемещая и не удаляя письма.'
    def add_arguments(self,parser):
        parser.add_argument('--require-trash',action='store_true',help='Завершить проверку ошибкой, если очистка не подтверждена.')

    def handle(self,*args,**options):
        problems=0
        for box in Mailbox.objects.filter(state='active'):
            try:
                with transport.connect(box) as client:
                    trash=any(f.get('trash') for f in transport.folders(client))
                    purge='UIDPLUS' in capabilities(client)
                    move='MOVE' in capabilities(client) or purge
                if not (trash and purge and move):problems+=1
                self.stdout.write(f'{box.address}: корзина {"есть" if trash else "не найдена"}; перемещение {"доступно" if move else "недоступно"}; очистка через 30 дней {"поддерживается" if purge and trash else "требует проверки сервера"}.')
            except ValidationError:
                problems+=1
                self.stdout.write(f'{box.address}: соединение не подтверждено; реквизиты не выводятся.')
        if problems and options['require_trash']:
            raise CommandError('Возможности корзины не подтверждены для всех ящиков. Сервисы запущены; требуется проверка сервера.')
