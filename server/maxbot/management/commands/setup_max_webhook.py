from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from maxbot.transport import api, DeliveryError


class Command(BaseCommand):
    help = 'Show webhook plan; --apply registers it at MAX using configured secrets.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        if not settings.ARMADA_MAX_ENABLED:
            raise CommandError('MAX отключён.')
        url = settings.ARMADA_MAX_PUBLIC_ORIGIN.rstrip('/') + '/integrations/max/webhook/'
        self.stdout.write('MAX webhook: ' + url)
        if not options['apply']:
            self.stdout.write('План подключения. Для регистрации подписки используйте --apply.')
            return
        try:
            bot = api('GET', '/me')
            if (not isinstance(bot, dict) or bot.get('is_bot') is not True
                    or str(bot.get('username', '')).lower() != settings.ARMADA_MAX_BOT_NAME.lower()):
                raise CommandError('Токен принадлежит другому боту или MAX не подтвердил имя. Подписка не изменена.')
            result = api('POST', '/subscriptions', {'url': url,
                'update_types': ['bot_started', 'message_created', 'message_callback'],
                'secret': settings.ARMADA_MAX_WEBHOOK_SECRET})
        except DeliveryError:
            raise CommandError('Подписка не подтверждена. Проверьте состояние MAX перед повтором; секреты не выводятся.') from None
        if not isinstance(result, dict) or result.get('success') is not True:
            raise CommandError('MAX не подтвердил подписку.')
        self.stdout.write('Подписка зарегистрирована. Выполните закрытый пилот.')
