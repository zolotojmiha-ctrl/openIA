from django.db import migrations,models

class Migration(migrations.Migration):
    dependencies=[('maxbot','0005_demandwatch')]
    operations=[migrations.AddField(model_name='demandwatch',name='format_version',field=models.PositiveSmallIntegerField(default=1)),
                migrations.AlterField(model_name='demandwatch',name='format_version',field=models.PositiveSmallIntegerField(default=2))]
