"""Read-only DNS check. DKIM must be issued by the SMTP signing provider."""
import re,shutil,subprocess
from django.core.management.base import BaseCommand,CommandError


class Command(BaseCommand):
    help='Проверить SPF/DMARC/DKIM домена. Пароли и токены не читаются.'
    def add_arguments(self,parser):
        parser.add_argument('--domain',default='armada-sibir.ru')
        parser.add_argument('--selector')
    def handle(self,*args,**options):
        domain=options['domain'];selector=options['selector']
        if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}',domain) or domain.startswith('-'):raise CommandError('Проверьте домен.')
        if selector and not re.fullmatch(r'[A-Za-z0-9_-]{1,63}',selector):raise CommandError('Проверьте селектор DKIM из Beget.')
        if not shutil.which('dig'):raise CommandError('Установите dnsutils или выполните dig на хосте VPS.')
        names=[('SPF',domain),('DMARC','_dmarc.'+domain)]
        if selector:names.append(('DKIM',selector+'._domainkey.'+domain))
        for label,name in names:
            result=subprocess.run(['dig','+time=5','+tries=1','TXT',name,'+short'],capture_output=True,text=True,timeout=10)
            raw=result.stdout.strip()
            self.stdout.write(label+': '+(raw if result.returncode==0 and raw else 'запись не получена'))
        if not selector:self.stdout.write('DKIM: нужен селектор и публичная запись от Beget. Приложение не может включить подпись на чужом SMTP-сервере.')
        self.stdout.write('DNS не подтверждает доставку. В новом письме Gmail «Показать оригинал» должны быть SPF PASS, DKIM PASS и DMARC PASS.')
