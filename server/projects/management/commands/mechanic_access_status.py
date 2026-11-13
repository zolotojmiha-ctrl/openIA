from django.core.management.base import BaseCommand
from accounts.models import User
from accounts.permissions import permitted


class Command(BaseCommand):
    help='Проверить фактические права механиков. Только чтение, без паролей и изменения доступов.'
    def handle(self,*args,**options):
        users=User.objects.filter(role='mechanic').order_by('full_name','pk')
        if not users.exists():self.stdout.write('Сотрудников с ролью «Механик» пока нет.');return
        for user in users:
            self.stdout.write(f'{user.full_name}: учётная запись {"активна" if user.is_active else "отключена"}; внесение техники и запчастей для ТО — {"разрешено" if permitted(user,"equipment.manage") else "не разрешено"}.')
            if user.must_change_password:self.stdout.write('  При первом входе требуется смена временного пароля.')
