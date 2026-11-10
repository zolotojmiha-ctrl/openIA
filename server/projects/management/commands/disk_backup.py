from pathlib import Path
from django.core.management.base import BaseCommand,CommandError
from projects import disk_backups,disk_crypto

class Command(BaseCommand):
    help='Обработчик копий Диска; восстановление выполняется отдельно с локальным закрытым ключом.'
    def add_arguments(self,parser):
        parser.add_argument('action',choices=['claim','upload','fail','decrypt'])
        parser.add_argument('--job')
        parser.add_argument('--source')
        parser.add_argument('--work')
        parser.add_argument('--private-key')
        parser.add_argument('--output')
        parser.add_argument('--encrypted',action='store_true')
        parser.add_argument('--key-id-file')
    def handle(self,*args,**o):
        try:
            if o['action']=='claim':
                pk=disk_backups.claim()
                if pk:self.stdout.write(str(pk))
            elif o['action']=='fail':disk_backups.fail(o['job'])
            elif o['action']=='upload':
                try:disk_backups.upload(o['job'],o['source'],o['work'],encrypted_key_id=Path(o['key_id_file']).read_text().strip() if o['encrypted'] else None)
                except Exception:
                    disk_backups.fail(o['job']);raise
                self.stdout.write('Копия загружена, повторно скачана и проверена.')
            else:
                # Offline mode: no API calls; private key stays on recovery operator's machine.
                if not all(o[k] for k in ('source','private_key','output')):raise ValueError
                disk_crypto.decrypt(o['source'],o['output'],Path(o['private_key']).read_bytes())
                self.stdout.write('Целостность расшифрованного архива подтверждена.')
        except Exception:
            raise CommandError('Операция не завершена. Проверьте настройки, файлы и результат в ЛК. Секреты и адреса загрузки не выводятся.') from None
