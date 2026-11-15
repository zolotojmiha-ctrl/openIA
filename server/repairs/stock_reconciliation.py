"""Explicit owner preview/confirmation of a missing posting for existing evidence."""
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from accounts.models import User
from accounts.views import audit
from projects import stock
from projects.models import StockItem
from .access import visible_jobs
from .models import Coupling, CouplingUse, Report, METHODS
from .installation_stock import choose, attach, source_key

SALT='repair-stock-reconciliation-v1'


def allowed(user):
    if not user.is_active or not user.is_leader:raise PermissionDenied
    return visible_jobs(user).filter(state__in=['reported','confirmed']).exclude(
        contract__status='archived').exclude(section__project_object__status='archived')


def pieces(job):
    result=[]
    for use in CouplingUse.objects.filter(job=job).select_related('coupling','report'):
        c=use.coupling
        if not c.source.get('reported_without_stock'):continue
        if stock.StockMovement.objects.filter(job=job,item__coupling=c,kind='consume',reversal__isnull=True).exists():continue
        result.append(c)
    return sorted(result,key=lambda c:str(c.pk))


def preview(user, job_id, sources):
    job=allowed(user).get(pk=job_id)
    rows=[];used={}
    for coupling in pieces(job):
        row=choose(job,coupling.method,coupling.length_mm,coupling.wall_mm,coupling.serial,
                   sources.get(str(coupling.pk)),used)
        if row.quantity!=1 and row.item.coupling_id:
            raise ValidationError('Некорректный остаток отдельной муфты.')
        used[(row.item_id,row.warehouse_id)]=used.get((row.item_id,row.warehouse_id),0)+1
        rows.append({'coupling':str(coupling.pk),'serial':coupling.serial,'method':coupling.method,
            'method_label':dict(METHODS).get(coupling.method,coupling.method),'length_mm':coupling.length_mm,'wall_mm':str(coupling.wall_mm),'item':str(row.item_id),
            'item_version':row.item.version,'warehouse':str(row.warehouse_id),'warehouse_name':row.warehouse.name,
            'balance':str(row.quantity),'previous_job':str(row.item.reserved_job_id or ''),
            'report':str(CouplingUse.objects.get(job=job,coupling=coupling).report_id)})
    if not rows:raise ValidationError('Неучтённых складских списаний по этой работе нет. Повторное списание не требуется.')
    facts={'actor':str(user.pk),'job':str(job.pk),'version':job.version,'sources':sources,'rows':rows}
    return facts,signing.dumps(facts,salt=SALT,compress=True)


@transaction.atomic
def confirm(user, token):
    actor=User.objects.select_for_update(no_key=True).get(pk=user.pk)
    if actor.session_version!=user.session_version:raise PermissionDenied
    allowed(actor)
    try:facts=signing.loads(token,salt=SALT,max_age=600)
    except signing.BadSignature:raise ValidationError('Предпросмотр устарел. Выполните сверку заново.')
    if facts['actor']!=str(actor.pk):raise PermissionDenied
    stock.lock()
    job=allowed(actor).select_for_update(of=('self',),no_key=True).get(pk=facts['job'])
    # Audited confirmations are idempotent without inventing another repair result.
    if not pieces(job):
        from accounts.models import AuditEvent
        if AuditEvent.objects.filter(action='repair_stock_reconciled',details__preview=token).exists():return job
    fresh,_=preview(actor,job.pk,facts['sources'])
    if fresh!=facts:raise ValidationError('Работа или склад изменились после предпросмотра. Проверьте новый подбор.')
    bindings=[]
    for saved in facts['rows']:
        coupling=Coupling.objects.select_for_update().get(pk=saved['coupling'])
        row=choose(job,coupling.method,coupling.length_mm,coupling.wall_mm,coupling.serial,source_key(saved['item'],saved['warehouse']))
        _,binding=attach(actor,job,row,coupling.serial,coupling)
        bindings.append(binding)
    reports={r['report'] for r in facts['rows']}
    all_couplings=list(Coupling.objects.filter(installation__job=job))
    # Each missing piece keeps its original report, date and installation evidence.
    for report_id in reports:
        report=Report.objects.get(pk=report_id,job=job)
        subset=[c for c in all_couplings if str(c.pk) in {r['coupling'] for r in facts['rows'] if r['report']==report_id}]
        # Include already posted pieces so reconcile never reverses unrelated consumption.
        posted=list(Coupling.objects.filter(stock_item__movements__job=job,
            stock_item__movements__kind='consume',stock_item__movements__reversal__isnull=True).distinct())
        stock.reconcile_installation(actor,job,report,list({c.pk:c for c in [*posted,*subset]}.values()))
        event=audit(actor,'repair_stock_reconciled',entity_id=str(job.pk),report_id=str(report.pk),
                    bindings=bindings,preview=token)
    job.version+=1;job.save(update_fields=['version'])
    return job
