from datetime import timedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from documents.storage import private_path
from maxbot.models import Inbox, Outbox, Draft, LinkTicket, RateBucket


class Command(BaseCommand):
    help = 'Queue counts; --cleanup removes expired draft photos and old event payloads owned by MAX.'

    def add_arguments(self, parser):
        parser.add_argument('--cleanup', action='store_true')

    def handle(self, *args, **options):
        if options['cleanup']:
            now = timezone.now()
            LinkTicket.objects.filter(expires__lt=now).delete()
            RateBucket.objects.exclude(key__endswith=':' + str(int(now.timestamp()) // 60)).delete()
            for pk in Draft.objects.filter(expires__lt=now).values_list('pk', flat=True).iterator():
                with transaction.atomic():
                    draft = Draft.objects.select_for_update().get(pk=pk)
                    keys = list(draft.photos.values_list('storage_key', flat=True))
                    draft.photos.all().delete()
                    draft.raw = {}
                    draft.fields = []
                    if draft.state != 'submitted':
                        draft.state = 'cancelled'
                    draft.save(update_fields=['state', 'raw', 'fields'])
                    transaction.on_commit(lambda keys=keys: [private_path(key).unlink(missing_ok=True) for key in keys])
            Inbox.objects.filter(created_at__lt=now-timedelta(days=7)).update(payload={})
            Outbox.objects.filter(expires__lt=now, state='pending').update(state='cancelled')
            Outbox.objects.filter(expires__lt=now-timedelta(days=7)).update(body={})
        for model in (Inbox, Outbox):
            self.stdout.write(model.__name__ + ': ' + str(list(model.objects.values('state').annotate(count=Count('pk')))))
