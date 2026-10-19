from decimal import Decimal
from django.db.models import Sum
from django.utils import timezone

ZERO=Decimal('0')

def total(qs,field='amount'):
    if any(f.name=='reversed' for f in qs.model._meta.fields):qs=qs.filter(reversed=False)
    return qs.aggregate(value=Sum(field))['value'] or ZERO

def summary(scope):
    base=getattr(scope,'opening',None)
    def start(key):return getattr(base,key,ZERO)
    advances=start('advance_received')+total(scope.allocations.filter(purpose='advance'))
    paid=start('paid_for_work')+total(scope.allocations.filter(purpose='work'))
    offsets=start('advance_offset')+total_advance_offsets(scope)
    accepted=start('accepted')+total(scope.executions.filter(accepted_by__isnull=False),'accepted_amount')
    factual_extra=total(scope.executions,'factual_amount')
    pending=total(scope.pending_work.filter(execution__isnull=True))+sum((work.factual_amount-total(work.offsets)-total(work.payments) for work in scope.executions.all()),ZERO)
    expected_balance=advances-offsets
    # Keep source balance explicitly separate: a declared historical balance may be rounded or erroneous.
    reported_balance=(start('declared_advance_balance')+advances-start('advance_received')-(offsets-start('advance_offset'))) if base else expected_balance
    pending_reported=start('declared_pending') if base else ZERO
    due_future=ZERO;due_overdue=ZERO;due_unknown=start('accepted')-start('paid_for_work')-start('advance_offset')-total(scope.allocations.filter(purpose='work',work__isnull=True))
    for work in scope.executions.filter(accepted_by__isnull=False):
        remaining=work.accepted_amount-total(work.offsets)-total(work.payments)
        if work.payment_due is None:due_unknown+=remaining
        elif work.payment_due<timezone.localdate():due_overdue+=remaining
        else:due_future+=remaining
    direct=total(scope.costs.filter(kind='direct'));overhead=total(scope.costs.filter(kind='overhead'))
    cash_out=total(scope.costs,'paid_amount')
    obligation=total(scope.subcontracts);subpaid=total(scope.subcontracts,'paid')
    price=None
    if scope.contract and scope.contract.amount is not None:price=scope.contract.amount+total(scope.contract.price_changes)
    return dict(scope=scope,base=base,advances=advances,paid=paid,offsets=offsets,accepted=accepted,pending=pending,pending_reported=pending_reported,
        expected_advance_balance=expected_balance,reported_advance_balance=reported_balance,advance_difference=reported_balance-expected_balance,
        pending_difference=pending_reported-total(scope.pending_work) if base else ZERO,accepted_unpaid=accepted-paid-offsets,
        due_future=due_future,due_overdue=due_overdue,due_unknown=due_unknown,direct=direct,overhead=overhead,cash_out=cash_out,
        subcontract_obligation=obligation,subcontract_paid=subpaid,subcontract_remaining=obligation-subpaid,
        # Subcontract commitments are NOT added to costs automatically, to avoid counting an entered cost twice.
        result=accepted-direct-overhead if scope.costs.exists() else None,cash_flow=advances+paid-cash_out if scope.costs.exists() and not base and not subpaid else None,
        cash_flow_new=total(scope.allocations)-cash_out if not subpaid else None,factual_extra=factual_extra,price=price,planned_profit=scope.planned_profit,
        progress=round(base.repaired_defects/base.total_defects*100,1) if base and base.total_defects else None,
        advance_used_percent=round(offsets/advances*100,1) if advances else 0,
        paid_percent=round(paid/accepted*100,1) if accepted else 0,offset_percent=round(offsets/accepted*100,1) if accepted else 0)

def total_advance_offsets(scope):
    from .models import AdvanceOffset
    return total(AdvanceOffset.objects.filter(work__scope=scope))
