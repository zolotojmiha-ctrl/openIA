import time,hashlib
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone
from postoffice.models import Letter,Mailbox
from postoffice.services import process_one
from postoffice import transport
from django.core.exceptions import ValidationError

class Command(BaseCommand):
    help='Обработать почтовую очередь; --loop для отдельного рабочего процесса.'
    def add_arguments(self,parser):parser.add_argument('--loop',action='store_true')
    def handle(self,*args,**options):
        ticks=0
        while True:
            close_old_connections()
            Letter.objects.filter(state='sending',updated_at__lt=timezone.now()-timedelta(minutes=10)).update(state='uncertain',error='Рабочий процесс прерван. Проверьте отправленные перед повторной отправкой.')
            for _ in range(20):
                try:
                    if not process_one():break
                except Exception:self.stderr.write('Не удалось обработать почтовую очередь; реквизиты и содержимое не выводятся.');break
            from postoffice.site_delivery import process_one as deliver_site_request, recover_interrupted, cleanup_expired
            try:
                recover_interrupted()
                if ticks%360==0:cleanup_expired()
                for _ in range(3):
                    if not deliver_site_request():break
            except Exception:self.stderr.write('Не удалось отправить уведомление о заявке с сайта; заявка сохранена в кабинете.')
            from postoffice.provider import process_one as create_mailbox
            try:create_mailbox()
            except Exception:self.stderr.write('Не удалось обработать создание ящика; секреты не выводятся.')
            if ticks%6==0:
                for box in Mailbox.objects.filter(state='active'):
                    try:
                        with transport.connect(box) as client:
                            transport.select(client,'INBOX');ids=transport.checked(client.uid('SEARCH',None,'UNSEEN','UNDELETED'))[0].split()
                        Mailbox.objects.filter(pk=box.pk,version=box.version).update(unread=len(ids),unread_digest=hashlib.sha256(b' '.join(ids)).hexdigest(),synced_at=timezone.now())
                    except ValidationError:continue
            if not options['loop']:break
            ticks+=1;time.sleep(10)
