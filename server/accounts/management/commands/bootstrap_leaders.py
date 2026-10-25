import getpass
import sys
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from accounts.models import User, AuditEvent, normalize_phone

class Command(BaseCommand):
    help='Создать двух равноправных руководителей через закрытый терминал. Секреты не выводятся.'
    def handle(self,*args,**options):
        if not sys.stdin.isatty():
            raise CommandError('Нужен защищённый интерактивный терминал; передача паролей аргументами запрещена.')
        if User.objects.filter(leader_slot__isnull=False).exists():
            raise CommandError('Руководители уже созданы. Повторное создание запрещено.')
        users=[]
        secrets=[]
        for slot,label in [(1,'генерального директора'),(2,'главного инженера')]:
            self.stdout.write(f'Закрытая настройка {label}. Введённые данные не будут выведены.')
            name=getpass.getpass('ФИО (скрытый ввод): ').strip()
            phone=getpass.getpass('Телефон (скрытый ввод): ')
            password=getpass.getpass('Уникальный временный пароль (не менее 6 символов): ')
            confirmation=getpass.getpass('Повторите пароль: ')
            if not name or len(name)>150 or password!=confirmation or password in secrets:
                raise CommandError('Проверьте ФИО, совпадение и независимость паролей.')
            try:
                user=User(phone=normalize_phone(phone),full_name=name,leader_slot=slot)
                validate_password(password,user)
                user.set_password(password)
                user.full_clean()
            except ValidationError:
                raise CommandError('Телефон, имя или пароль не прошли проверку. Используйте уникальную длинную парольную фразу и корректный телефон.')
            users.append(user)
            secrets.append(password)
        if users[0].phone==users[1].phone:
            raise CommandError('Руководителям нужны разные телефоны.')
        with transaction.atomic():
            for user in users:
                user.save()
                AuditEvent.objects.create(actor=user,subject=user,action='leader_bootstrap')
        self.stdout.write(self.style.SUCCESS('Созданы два равноправных руководителя. При первом входе обязательна смена пароля.'))
