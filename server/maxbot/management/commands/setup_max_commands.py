"""Register a discoverable command without exposing credentials or changing subscriptions."""
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from maxbot.transport import api,DeliveryError

MENU={'name':'menu','description':'Меню — все доступные функции'}


def merged(commands):
    if commands is None:commands=[]
    if not isinstance(commands,list) or any(not isinstance(c,dict) or not isinstance(c.get('name'),str) for c in commands):
        raise CommandError('MAX не подтвердил прежний список команд. Ничего не изменено.')
    others=[{key:value for key,value in c.items() if key in ('name','description')} for c in commands if c['name'].lstrip('/')!='menu']
    if len(others)>=32:raise CommandError('В MAX уже заполнены все 32 команды. Ничего не изменено.')
    return [MENU.copy(),*others]


class Command(BaseCommand):
    help='Показать план меню команд MAX; --apply добавляет /menu с сохранением других команд.'
    def add_arguments(self,parser):parser.add_argument('--apply',action='store_true')
    def handle(self,*args,**options):
        self.stdout.write('/menu — Меню: все доступные функции по вашей роли.')
        if not options['apply']:
            self.stdout.write('План. Для настройки MAX используйте --apply.');return
        if not settings.ARMADA_MAX_ENABLED:raise CommandError('MAX отключён.')
        try:
            bot=api('GET','/me')
            if not isinstance(bot,dict) or bot.get('is_bot') is not True or str(bot.get('username','')).lower()!=settings.ARMADA_MAX_BOT_NAME.lower():
                raise CommandError('Токен принадлежит другому боту. Команды не изменены.')
            commands=merged(bot.get('commands'))
            result=api('PATCH','/me/commands',{'commands':commands})
            if not isinstance(result,dict) or not isinstance(result.get('commands'),list):raise CommandError('MAX не подтвердил изменение. Проверьте меню перед повтором.')
            actual={c.get('name','').lstrip('/'):c.get('description') for c in result['commands'] if isinstance(c,dict)}
            if actual.get('menu')!=MENU['description']:raise CommandError('MAX не подтвердил пункт «Меню». Проверьте состояние перед повтором.')
        except DeliveryError:
            raise CommandError('Настройка команд не подтверждена. Сайт и бот продолжают работать; проверьте меню перед повтором. Секреты не выводятся.') from None
        self.stdout.write('Меню команд MAX настроено. Откройте список команд и выберите /menu — «Меню — все доступные функции». Отображение кнопки зависит от версии приложения MAX.')
