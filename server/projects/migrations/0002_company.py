from django.db import migrations

def add_company(apps,schema_editor):
    apps.get_model('projects','LegalEntity').objects.get_or_create(tax_id='8604073687',defaults={'name':'ООО «Армада-Сибирь»'})

class Migration(migrations.Migration):
    dependencies=[('projects','0001_initial')]
    operations=[migrations.RunPython(add_company,migrations.RunPython.noop)]
