from django.db import migrations

def forward(apps,schema_editor):
    Document=apps.get_model('documents','Document');Person=apps.get_model('documents','Person');User=apps.get_model('accounts','User')
    Link=apps.get_model('documents','DocumentPerson')
    valid={'contracts','personnel','journals','as_built','acts','drawings','media','general'}
    Document.objects.exclude(section__in=valid).update(section='general')
    Document.objects.filter(kind='personal').update(section='personnel')
    for link in Link.objects.select_related('document','person'):
        user=link.person
        person,_=Person.objects.get_or_create(user_id=user.pk,defaults={'full_name':user.full_name,'legal_entity_id':link.document.legal_entity_id,'created_by_id':link.document.created_by_id,'archived':not user.is_active,'position':dict(User._meta.get_field('role').choices).get(user.role,user.role)})
        link.document.personnel_linked.add(person)

class Migration(migrations.Migration):
    dependencies=[('documents','0002_document_is_primary_document_link_not_required_and_more')]
    operations=[migrations.RunPython(forward,migrations.RunPython.noop)]
