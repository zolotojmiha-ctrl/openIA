import time
from django.conf import settings
from django.db import close_old_connections
from django.core.management.base import BaseCommand, CommandError
from maxbot.worker import tick


class Command(BaseCommand):
    help = 'Process durable MAX events. Network delivery requires --deliver; disabled by default.'

    def add_arguments(self, parser):
        parser.add_argument('--loop', action='store_true')
        parser.add_argument('--deliver', action='store_true')

    def handle(self, *args, **options):
        if not settings.ARMADA_MAX_ENABLED:
            raise CommandError('MAX отключён. Настройте интеграцию перед запуском worker.')
        while True:
            try:
                close_old_connections()
                count = tick(deliver_messages=options['deliver'])
                if not options['loop']:
                    self.stdout.write(f'Processed MAX events: {count}')
                    return
            except Exception:
                # Never print raw exception: upstream errors can include signed URLs.
                self.stderr.write('MAX worker failed; inspect queue states. Payloads are not logged.')
                if not options['loop']:
                    raise CommandError('MAX queue processing failed.') from None
            time.sleep(2)
