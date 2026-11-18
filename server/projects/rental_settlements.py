"""Explicit invoice settlement; transfer dates never imply service coverage."""
from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from accounts.views import audit
from . import rentals
from .models import RentalInvoice, RentalPaymentAllocation

ZERO=Decimal('0')


def amount(rows):
    return rows.aggregate(v=Sum('amount'))['v'] or ZERO


def payment_credit(payment, on=None):
    uses=payment.deposit_uses.filter(reversed=False,kind='offset')
    if on:uses=uses.filter(date__lte=on)
    return ZERO if payment.reversed else payment.amount-payment.deposit_amount+amount(uses)


def allocated(payment, on=None):
    rows=payment.allocations.filter(reversed=False,invoice__void_reason='',payment__reversed=False)
    if on:rows=rows.filter(date__lte=on)
    return amount(rows)


def invoice_balance(invoice, on=None, paid_amount=None):
    target=Decimal(invoice.period.snapshot['period_charged'])
    rows=invoice.allocations.filter(reversed=False,payment__reversed=False)
    if on:rows=rows.filter(date__lte=on)
    paid=(amount(rows) if paid_amount is None else paid_amount) if not invoice.void_reason else ZERO
    return {'charged':target,'supplier_amount':invoice.supplier_amount,
        'difference':invoice.supplier_amount-target,'paid':paid,'debt':max(ZERO,target-paid)}


@transaction.atomic
def create_invoice(user,pk,start,end,issued_on,reference,supplier_amount,token,note=''):
    actor,rental=rentals.financial_lock(user,pk)
    supplier_amount=rentals.money(supplier_amount)
    if supplier_amount<0 or not reference.strip() or len(reference)>200 or not end<=issued_on<=timezone.localdate() or len(note)>500:
        raise ValidationError('Проверьте дату, номер и сумму документа.')
    old=RentalInvoice.objects.filter(token=token).select_related('period').first()
    if old:
        if (old.period.rental_id,old.period.starts_on,old.period.ends_on,old.issued_on,old.reference,old.supplier_amount,old.period.note)!=(rental.pk,start,end,issued_on,reference,supplier_amount,note):
            raise ValidationError('Эта отправка уже использована для другой сверки.')
        return old
    period=rentals.close_period(actor,rental.pk,start,end,note)
    invoice=RentalInvoice.objects.create(period=period,reference=reference,issued_on=issued_on,supplier_amount=supplier_amount,token=token,created_by=actor)
    audit(actor,'finance_created',kind='rental_invoice',invoice=str(invoice.pk),period=str(period.pk))
    return invoice


@transaction.atomic
def allocate(user,pk,payment_id,invoice_id,date,amount,note,token):
    actor,rental=rentals.financial_lock(user,pk)
    payment=rental.payments.select_for_update().get(pk=payment_id)
    invoice=RentalInvoice.objects.select_for_update().select_related('period').get(pk=invoice_id,period__rental=rental)
    value=rentals.money(amount)
    if value<=0 or not max(payment.date,invoice.issued_on)<=date<=timezone.localdate() or not note.strip() or len(note)>500:
        raise ValidationError('Проверьте сумму, дату зачёта и основание.')
    old=RentalPaymentAllocation.objects.filter(token=token).first()
    if old:
        if (old.payment_id,old.invoice_id,old.date,old.amount,old.note)!=(payment.pk,invoice.pk,date,value,note):
            raise ValidationError('Эта отправка уже использована для другого зачёта.')
        return old
    if payment.reversed or not payment.purpose_confirmed or invoice.void_reason or invoice.period.reopened_at:
        raise ValidationError('Подтвердите назначения платежа и действующую сверку.')
    balance=invoice_balance(invoice)
    if balance['difference']:
        raise ValidationError('Сумма документа отличается от табеля. Уточните счёт и сохраните новую сверку перед оплатой.')
    # Both whole-ledger and as-of bounds matter when accounting is backdated.
    if value>balance['debt'] or value>payment_credit(payment)-allocated(payment) or value>payment_credit(payment,date)-allocated(payment,date):
        raise ValidationError('Сумма превышает остаток платежа или долг по счёту.')
    # A backdated allocation must also preserve every later historical balance.
    later_dates=set(payment.deposit_uses.filter(kind='offset',reversed=False,date__gt=date).values_list('date',flat=True))
    later_dates.update(payment.allocations.filter(reversed=False,date__gt=date).values_list('date',flat=True))
    for later in later_dates:
        if allocated(payment,later)+value>payment_credit(payment,later):
            raise ValidationError('Зачёт нарушает остаток платежа на последующие даты.')
    result=RentalPaymentAllocation.objects.create(payment=payment,invoice=invoice,date=date,amount=value,note=note,token=token,created_by=actor)
    audit(actor,'finance_created',kind='rental_invoice_allocation',allocation=str(result.pk),amount=str(value))
    return result


@transaction.atomic
def reverse_allocation(user,pk,allocation_id,reason):
    actor,rental=rentals.financial_lock(user,pk)
    if not reason.strip() or len(reason)>500:raise ValidationError('Укажите причину отмены.')
    row=RentalPaymentAllocation.objects.select_for_update().get(pk=allocation_id,payment__rental=rental,invoice__period__rental=rental)
    if row.reversed:return row
    row.reversed=True;row.reversal_reason=reason;row.save(update_fields=['reversed','reversal_reason'])
    audit(actor,'finance_corrected',kind='rental_allocation_reversed',allocation=str(row.pk),reason=reason)
    return row


@transaction.atomic
def void_invoice(user,pk,invoice_id,reason):
    actor,rental=rentals.financial_lock(user,pk)
    if not reason.strip() or len(reason)>500:raise ValidationError('Укажите причину отмены сверки.')
    row=RentalInvoice.objects.select_for_update().get(pk=invoice_id,period__rental=rental)
    if row.void_reason:return row
    if row.allocations.filter(reversed=False).exists():raise ValidationError('Сначала отмените зачёты оплаты по этому счёту.')
    row.void_reason=reason;row.save(update_fields=['void_reason'])
    rentals.reopen_period(actor,rental.pk,row.period_id,reason)
    audit(actor,'finance_corrected',kind='rental_invoice_void',invoice=str(row.pk),reason=reason)
    return row


def summary(rental,on=None):
    on=on or timezone.localdate()
    rows=RentalInvoice.objects.filter(period__rental=rental,void_reason='',period__reopened_at__isnull=True,period__ends_on__lte=on,issued_on__lte=on).select_related('period').order_by('period__starts_on')
    payments_by_invoice=dict(RentalPaymentAllocation.objects.filter(invoice__period__rental=rental,payment__reversed=False,reversed=False,date__lte=on).values('invoice_id').annotate(total=Sum('amount')).values_list('invoice_id','total'))
    start=rental.placement.start_date
    debt=ZERO
    invoices=[]
    for row in rows:
        balance=invoice_balance(row,on,paid_amount=payments_by_invoice.get(row.pk,ZERO))
        debt+=balance['debt'];invoices.append((row,balance))
        if row.period.starts_on==start and balance['debt']==0 and not balance['difference']:
            start=row.period.ends_on+timedelta(days=1)
    days=rental.days.filter(date__gte=start,date__lte=on)
    paid=rentals.totals(rental,on)
    credited=RentalPaymentAllocation.objects.filter(payment__rental=rental,payment__reversed=False,invoice__void_reason='',reversed=False,date__lte=on)
    unallocated=max(ZERO,paid['paid']-amount(credited))
    return {'invoices':invoices,'invoice_debt':debt,'next_start':start,'paid_through':start-timedelta(days=1) if start>rental.placement.start_date else None,'new_charged':amount(days),
        'new_hours':days.aggregate(v=Sum('hours'))['v'] or 0,'unallocated':unallocated}
