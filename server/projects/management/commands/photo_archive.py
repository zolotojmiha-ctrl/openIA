from django.core.management.base import BaseCommand,CommandError
from projects import photo_archive
from projects.models import DiskBackupSettings,PhotoArchive


class Command(BaseCommand):
    help='Состояние приватных фото или ограниченная обработка по настройкам ЛК. Не включает удаление самостоятельно.'
    def add_arguments(self,parser):
        parser.add_argument('action',choices=['status','process'])
        parser.add_argument('--limit',type=int,default=1)
    def handle(self,*args,**options):
        if not 1<=options['limit']<=50:raise CommandError('Лимит от 1 до 50.')
        if options['action']=='process':
            try:
                for _ in range(options['limit']):
                    if not photo_archive.step():break
            except Exception:raise CommandError('Копирование не завершено. Проверьте подключение Диска; непроверенные локальные оригиналы не удаляются.') from None
        config=DiskBackupSettings.objects.filter(pk=1).first()
        self.stdout.write('Копирование фото: '+str(bool(config and config.photos_enabled)))
        self.stdout.write('Освобождение места: '+str(bool(config and config.photos_remove_local)))
        self.stdout.write('Проверено фото: '+str(PhotoArchive.objects.count()))
        self.stdout.write('Локальные копии удалены: '+str(PhotoArchive.objects.filter(local_removed_at__isnull=False).count()))
