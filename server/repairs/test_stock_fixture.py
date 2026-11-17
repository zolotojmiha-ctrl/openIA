"""Current stock-backed synthetic coupling fixtures for legacy repair scenarios."""
import uuid
from django.utils import timezone
from projects import stock,stock_planning
from projects.models import StockItem


def receive(leader,job,length_mm,wall_mm,count=1,coupling=None):
    warehouse=stock.save_warehouse(leader,None,None,{'name':'TEST '+uuid.uuid4().hex,'location':'TEST','project_object':job.section.project_object,'brigade_assignment':None,'archived':False})
    before=set(StockItem.objects.values_list('pk',flat=True))
    stock_planning.receive(leader,{'sku':uuid.uuid4().hex,'name':'TEST coupling','kind':'coupling','unit':'pcs','method':job.method,'diameter_mm':job.section.diameter_mm,'length_mm':length_mm,'wall_mm':wall_mm,'coupling':None,'archived':False},warehouse,count,timezone.localdate(),'TEST',uuid.uuid4())
    items=list(StockItem.objects.exclude(pk__in=before))
    if coupling:
        items[0].coupling=coupling;items[0].serial=coupling.serial;items[0].save(update_fields=['coupling','serial'])
    return items
