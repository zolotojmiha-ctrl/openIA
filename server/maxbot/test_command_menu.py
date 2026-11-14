from io import StringIO
from unittest.mock import patch
from django.test import SimpleTestCase,TestCase,override_settings
from django.core.management import call_command,CommandError
from . import tests as base
from projects import test_stock as fixture
from .models import Outbox


@override_settings(**base.CONFIG)
class CommandMenuTests(SimpleTestCase):
    def test_plan_never_calls_api(self):
        with patch('maxbot.management.commands.setup_max_commands.api') as api:
            call_command('setup_max_commands',stdout=StringIO());api.assert_not_called()

    def test_registration_preserves_other_commands_and_verifies_result(self):
        previous={'is_bot':True,'username':base.CONFIG['ARMADA_MAX_BOT_NAME'],'commands':[{'name':'help','description':'Помощь'}]}
        with patch('maxbot.management.commands.setup_max_commands.api',side_effect=[previous,{'commands':[{'name':'menu','description':'Меню — все доступные функции'},{'name':'help','description':'Помощь'}]}]) as api:
            call_command('setup_max_commands',apply=True,stdout=StringIO())
            self.assertEqual(api.call_args.args,('PATCH','/me/commands',{'commands':[{'name':'menu','description':'Меню — все доступные функции'},{'name':'help','description':'Помощь'}]}))

    def test_wrong_bot_and_unconfirmed_response_fail(self):
        for replies in ([{'is_bot':True,'username':'another_bot'}],
                        [{'is_bot':True,'username':base.CONFIG['ARMADA_MAX_BOT_NAME']},{'commands':[]}]):
            with patch('maxbot.management.commands.setup_max_commands.api',side_effect=replies) as api,self.assertRaises(CommandError):
                call_command('setup_max_commands',apply=True,stdout=StringIO())
            if len(replies)==1:self.assertEqual(api.call_count,1)


@override_settings(**base.CONFIG)
class MasterMenuTests(TestCase):
    setUpTestData=classmethod(fixture.StockTests.setUpTestData.__func__)
    setUp=base.MaxTests.setUp
    bind=base.MaxTests.bind
    event=base.MaxTests.event
    def test_latin_and_russian_menu_open_actions_without_starting_report(self):
        from .models import Draft
        for command in ('/menu','/меню'):
            event=self.event(command)
            row=Outbox.objects.get(key=f'in:{event.pk}:reply')
            buttons=row.body['attachments'][0]['payload']['buttons']
            self.assertEqual([b[0]['payload'] for b in buttons],['/фото','/работы','ddk:list:0','/архив','/устранённые','/техника'])
            self.assertFalse(Draft.objects.exists())
