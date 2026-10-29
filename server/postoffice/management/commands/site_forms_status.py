from django.core.management.base import BaseCommand
from postoffice.models import Mailbox, SiteRequest
from postoffice.site_forms import RECIPIENT
from postoffice.crypto import reveal
from django.core.exceptions import ValidationError


class Command(BaseCommand):
    help='Проверить настройку приёма обращений без отправки писем и вывода секретов.'

    def handle(self,*args,**options):
        box=Mailbox.objects.filter(address__iexact=RECIPIENT,state='active').exclude(credential='').first()
        ready=False
        if box:
            try:reveal(box.credential);ready=True
            except ValidationError:pass
        self.stdout.write('Получатель обращений: '+RECIPIENT)
        self.stdout.write('Приём: обращения сохраняются в кабинете → Почта → Заявки с сайта.')
        self.stdout.write('Реквизиты info и ключ шифрования: '+('настроены' if ready else 'НУЖНО ПОДКЛЮЧИТЬ INFO В РАЗДЕЛЕ ПОЧТА'))
        self.stdout.write('Проверка выше не отправляет письмо и не подтверждает доступность SMTP.')
        self.stdout.write('Очередь: '+str(SiteRequest.objects.filter(delivery_state='queued').count()))
        self.stdout.write('Требуют проверки доставки: '+str(SiteRequest.objects.filter(delivery_state__in=['failed','uncertain']).count()))
