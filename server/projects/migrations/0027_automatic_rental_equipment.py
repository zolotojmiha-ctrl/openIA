from django.db import migrations


def forwards(apps,schema_editor):
    Assignment=apps.get_model('projects','EquipmentAssignment');Rental=apps.get_model('projects','EquipmentRental');Scope=apps.get_model('finance','FinancialScope')
    db=schema_editor.connection.alias
    for p in Assignment.objects.using(db).filter(equipment__ownership='rent',equipment__legal_entity__tax_id='8604073687',end_date__isnull=True,rental__isnull=True).select_related('equipment').iterator():
        scopes=list(Scope.objects.using(db).filter(contract_id=p.contract_id).values_list('pk',flat=True)[:2]) if p.contract_id else []
        Rental.objects.using(db).get_or_create(placement_id=p.pk,defaults={'supplier':p.equipment.owner,'reference':'','financial_scope_id':scopes[0] if len(scopes)==1 else None,'created_by_id':p.created_by_id})

class Migration(migrations.Migration):
    dependencies=[('projects','0026_alter_equipment_ownership')]
    operations=[migrations.RunPython(forwards,migrations.RunPython.noop)]
