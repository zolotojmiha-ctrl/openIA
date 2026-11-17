import json
from django.core.management.base import BaseCommand,CommandError
from django.core.exceptions import ValidationError,PermissionDenied
from repairs import photo_cleanup


class Command(BaseCommand):
    help='Preview exact photo duplicates; apply only with the displayed plan SHA-256. No automatic production invocation.'
    def add_arguments(self,parser):
        parser.add_argument('--apply',metavar='PLAN_SHA256')
        parser.add_argument('--cleanup',action='store_true')
    def handle(self,*args,**options):
        if options['cleanup']:
            photo_cleanup.step();self.stdout.write('One cleanup step completed.');return
        try:
            plan=photo_cleanup.consolidate(options['apply']) if options['apply'] else photo_cleanup.duplicate_plan()
        except (ValidationError,PermissionDenied) as error:raise CommandError(' '.join(error.messages)) from None
        self.stdout.write(json.dumps({'plan_sha256':photo_cleanup.plan_digest(plan),'maximum_reclaimable_bytes':sum(len(row['duplicates'])*row['size'] for row in plan),'groups':plan,'applied':bool(options['apply']),'notice':'Shared references and external archive can delay physical reclamation. Archived contracts are protected.'},ensure_ascii=False,indent=2))
