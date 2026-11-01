from django.db import migrations


def grant_stock(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    Grant = apps.get_model('accounts', 'AccessGrant')
    database = schema_editor.connection.alias
    staff = User.objects.using(database).filter(role='pto', leader_slot__isnull=True)
    if not staff.exists():
        return
    leader = User.objects.using(database).filter(leader_slot__in=[1, 2]).order_by('leader_slot').first()
    if leader is None:
        raise RuntimeError('Для выдачи ПТО складских прав нужна существующая учётная запись руководителя.')
    for user in staff.iterator():
        Grant.objects.using(database).get_or_create(
            user_id=user.pk, permission='stock.manage', defaults={'granted_by_id': leader.pk})


class Migration(migrations.Migration):
    dependencies = [('accounts', '0027_alter_accessgrant_permission')]
    operations = [migrations.RunPython(grant_stock, migrations.RunPython.noop)]
