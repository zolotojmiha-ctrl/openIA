from django.db import migrations
from django.db.models import F

def forward(apps,schema_editor):
    apps.get_model('projects','StockItem').objects.filter(kind__in=['composite','sealant'],unit='pcs').update(unit='kit',version=F('version')+1)

def reverse(apps,schema_editor):
    apps.get_model('projects','StockItem').objects.filter(kind__in=['composite','sealant'],unit='kit').update(unit='pcs',version=F('version')+1)

class Migration(migrations.Migration):
    dependencies=[('projects','0023_rental_materials_customer624')]
    operations=[migrations.RunPython(forward,reverse)]
