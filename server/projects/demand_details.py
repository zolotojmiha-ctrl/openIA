"""Display-only locations for demand lines; one construction is never multiplied by defects."""
from decimal import Decimal


def location(job):
    defects=sorted(job.active_defects,key=lambda d:(d.kilometre,d.number,str(d.pk)))
    return {'job_id':str(job.pk),'kilometres':'; '.join(dict.fromkeys(d.km for d in defects)) or '—',
            'defect_numbers':'; '.join(d.number for d in defects) or '—',
            'pipe_sections':'; '.join(dict.fromkeys(d.pipe_section for d in defects if d.pipe_section)) or '—',
            'pipeline':job.section.pipeline,'rnu':job.section.rnu,'state':job.get_state_display()}


def order(row):
    km=row['kilometres'].split(';')[0]
    return (Decimal(km) if km!='—' else Decimal('Infinity'),row['defect_numbers'],row['job_id'])
