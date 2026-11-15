"""Read-only shortage snapshot; aggregate automatic alerts have been retired."""
import hashlib,json
from decimal import Decimal
from django.core.exceptions import ValidationError
from projects.repair_materials import all_demand
from repairs.access import visible_jobs
from .models import Outbox
from .leadership import contract_name


def snapshot(user):
    allowed=set(visible_jobs(user).values_list('pk',flat=True));rows=[]
    for record in all_demand(user):
        if record.get('allocation_failed'):
            raise ValidationError('Совместный подбор не завершён.')
        job=record['job']
        if job.pk not in allowed:continue
        for line in record['couplings']:
            if line['missing']:
                rows.append({'job':str(job.pk),'contract':contract_name(user,job.contract),'object':job.section.project_object.name,
                    'section':job.section.name,'contract_id':str(job.contract_id or ''),'object_id':str(job.section.project_object_id),'section_id':str(job.section_id),
                    'diameter':job.section.diameter_mm,'method':line['method'],'length':line['length'],'wall':format(Decimal(str(line['wall'])).normalize(),'f') if line['wall'] is not None else '','quantity':line['missing']})
    rows.sort(key=lambda r:(r['job'],r['method'],r['length'],r['wall']))
    facts=[{k:v for k,v in row.items() if k not in ('contract','object','section')} for row in rows]
    digest=hashlib.sha256(json.dumps(facts,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return digest,rows


def queue_changes():
    """Retire aggregate alerts; concrete ledger notices are queued with reports."""
    Outbox.objects.filter(key__startswith='shortage:', state='pending').update(state='cancelled')
