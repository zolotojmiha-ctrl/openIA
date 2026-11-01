"""Payable rental hours are a ledger, not engine meter readings."""
from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from accounts.permissions import permitted
from accounts.models import User
from accounts.views import audit
from .models import (EquipmentRental, EquipmentAssignment, RentalRate, RentalDay,
 RentalDayRevision, RentalPayment, RentalPeriod, COMPANY_TAX_ID)

ZERO=Decimal('0')
HOURS={'work':10,'idle':4,'external':0}

def manager(user):
    return bool(user.is_authenticated and user.is_active and user.role!='customer' and (user.is_leader or permitted(user,'rental.manage') or permitted(user,'finance.write')))

def financial_writer(user):
    return user.is_authenticated and user.is_active and user.role!='customer' and (user.is_leader or permitted(user,'finance.write'))

def financial_reader(user):
    return manager(user) or bool(user.is_authenticated and user.is_active and user.role!='customer' and permitted(user,'finance.read'))

def visible(user):
    rows=EquipmentRental.objects.filter(placement__equipment__legal_entity__tax_id=COMPANY_TAX_ID).select_related('placement__equipment','placement__project_object','placement__crew_assignment__brigade','placement__contract','agreement')
    if not user.is_authenticated or not user.is_active or user.role=='customer':return rows.none()
    if financial_reader(user):return rows
    allowed=rows.filter(Q(placement__crew_assignment__master=user)|Q(placement__equipment__custodies__brigade__assignments__master=user))
    return rows.filter(pk__in=allowed.values('pk'))

def allowed_on(user,rental,date):
    if manager(user):return True
    placement=rental.placement
    if not user.is_active:return False
    crew=placement.crew_assignment
    if crew and crew.master_id==user.pk and crew.start_date<=date and (not crew.end_date or date<=crew.end_date):return True
    return placement.equipment.custodies.filter(start_date__lte=date).filter(Q(end_date__isnull=True)|Q(end_date__gte=date)).filter(brigade__assignments__master=user,brigade__assignments__project_object=placement.project_object,brigade__assignments__start_date__lte=date).filter(Q(brigade__assignments__end_date__isnull=True)|Q(brigade__assignments__end_date__gte=date)).exists()

def period_dates(rental,start,end):
    p=rental.placement
    if not start or not end or start>end or start<p.start_date or (p.end_date and end>p.end_date) or end>timezone.localdate():raise ValidationError('Дата должна входить в период работы этой техники и не быть в будущем.')

def actor_lock(user,require_manager=False):
    actor=User.objects.select_for_update().get(pk=user.pk)
    if not actor.is_active or actor.session_version!=user.session_version or require_manager and not manager(actor):raise PermissionDenied
    return actor

@transaction.atomic
def configure(user,placement_id,supplier,reference,agreement=None,financial_scope=None):
    actor=actor_lock(user,True)
    from .equipment import visible_equipment
    placement=EquipmentAssignment.objects.select_for_update().select_related('equipment').get(pk=placement_id,equipment__in=visible_equipment(actor))
    if placement.equipment.ownership!='rent':raise ValidationError('Денежный учёт аренды включается для арендованной техники.')
    if not supplier.strip() or not reference.strip():raise ValidationError('Укажите арендодателя и договор.')
    if agreement and (agreement.kind!='equipment_rent' or agreement.equipment_id not in (None,placement.equipment_id)):raise ValidationError('Договор относится к другой технике или другому виду услуг.')
    if financial_scope and (financial_scope.legal_entity.tax_id!=COMPANY_TAX_ID or financial_scope.contract_id!=placement.contract_id):raise ValidationError('Финансовый учёт должен относиться к контракту назначения техники.')
    if not financial_scope and placement.contract_id:
        from finance.models import FinancialScope
        candidates=list(FinancialScope.objects.filter(contract_id=placement.contract_id)[:2])
        if len(candidates)==1:financial_scope=candidates[0]
    row,created=EquipmentRental.objects.get_or_create(placement=placement,defaults={'supplier':supplier.strip(),'reference':reference.strip(),'agreement':agreement,'financial_scope':financial_scope,'created_by':actor})
    if not created:raise ValidationError('Учёт аренды этого назначения уже создан. Тарифы и оплаты меняются в его карточке.')
    audit(actor,'equipment_assigned',kind='rental_created',rental=str(row.pk))
    return row

@transaction.atomic
def add_rate(user,pk,starts_on,basis,work_price,idle_price):
    if not financial_writer(user):raise PermissionDenied
    actor=actor_lock(user,True)
    if not financial_writer(actor):raise PermissionDenied
    rental=visible(actor).select_for_update(of=('self',)).get(pk=pk)
    if basis not in ('day','hour') or min(work_price,idle_price)<0:raise ValidationError('Проверьте тарифы.')
    if starts_on<rental.placement.start_date or rental.placement.end_date and starts_on>rental.placement.end_date:raise ValidationError('Тариф должен входить в период назначения.')
    if rental.days.filter(date__gte=starts_on,rate__isnull=False).exists() or rental.periods.filter(ends_on__gte=starts_on,reopened_at__isnull=True).exists():raise ValidationError('За эти даты уже есть табель. Новый тариф задаётся с даты после внесённых отметок; история не пересчитывается.')
    if rental.rates.filter(starts_on=starts_on).exists():raise ValidationError('Тариф с этой даты уже есть.')
    row=RentalRate.objects.create(rental=rental,starts_on=starts_on,basis=basis,work_price=work_price,idle_price=idle_price,created_by=actor)
    next_date=rental.rates.filter(starts_on__gt=starts_on).order_by('starts_on').values_list('starts_on',flat=True).first()
    unpriced=rental.days.filter(date__gte=starts_on,rate__isnull=True,hours__gt=0)
    if next_date:unpriced=unpriced.filter(date__lt=next_date)
    for day in unpriced.select_for_update():
        day.rate=row;day.amount=(work_price if day.kind=='work' else idle_price)*(day.hours if basis=='hour' else 1);day.version+=1
        day.save(update_fields=['rate','amount','version'])
        RentalDayRevision.objects.create(day=day,number=day.version,data={'kind':day.kind,'hours':day.hours,'amount':str(day.amount),'rate':row.pk,'note':'Начисление по первому заданному тарифу'},author=actor)
    rental.version+=1;rental.save(update_fields=['version']);return row

@transaction.atomic
def save_day(user,pk,date,kind,version=0,note=''):
    actor=actor_lock(user);rental=visible(actor).select_for_update(of=('self',)).get(pk=pk)
    period_dates(rental,date,date)
    if not allowed_on(actor,rental,date):raise PermissionDenied
    if kind not in HOURS:raise ValidationError('Выберите 10, 4 или 0 часов.')
    if rental.periods.filter(starts_on__lte=date,ends_on__gte=date,reopened_at__isnull=True).exists():raise ValidationError('Период закрыт. Для изменения нужна корректировка периода руководителем.')
    row=rental.days.select_for_update().filter(date=date).first()
    if row and row.kind==kind:return row
    if row and row.version!=version:raise ValidationError('Этот день уже изменён. Обновите табель.')
    if row and not note.strip():raise ValidationError('Для исправления укажите короткую причину.')
    rate=rental.rates.filter(starts_on__lte=date).order_by('-starts_on').first()
    hours=HOURS[kind]
    amount=ZERO if not hours or not rate else (rate.work_price if kind=='work' else rate.idle_price)*(hours if rate.basis=='hour' else 1)
    if not row:row=RentalDay(rental=rental,date=date,version=0)
    row.kind=kind;row.hours=hours;row.amount=amount;row.rate=rate;row.updated_by=actor;row.version+=1;row.save()
    RentalDayRevision.objects.create(day=row,number=row.version,data={'kind':kind,'hours':hours,'amount':str(amount),'rate':rate.pk if rate else None,'note':note},author=actor)
    audit(actor,'equipment_assigned',kind='rental_day',rental=str(pk),date=str(date),hours=hours)
    return row

@transaction.atomic
def payment(user,pk,date,amount,note,token):
    if not financial_writer(user):raise PermissionDenied
    actor=actor_lock(user,True)
    if not financial_writer(actor):raise PermissionDenied
    rental=visible(actor).select_for_update(of=('self',)).get(pk=pk)
    if amount<=0 or date>timezone.localdate() or not note.strip():raise ValidationError('Укажите положительную сумму, фактическую дату и основание.')
    old=RentalPayment.objects.filter(token=token).first()
    if old:
        if old.rental_id!=rental.pk or old.date!=date or old.amount!=amount or old.note!=note:raise ValidationError('Эта отправка уже использована для другого платежа.')
        return old
    row=RentalPayment.objects.create(rental=rental,date=date,amount=amount,note=note,token=token,created_by=actor)
    audit(actor,'finance_created',kind='rental_payment',payment=str(row.pk),rental=str(pk),amount=str(amount));return row

@transaction.atomic
def reverse_payment(user,pk,payment_id,reason):
    if not financial_writer(user):raise PermissionDenied
    actor=actor_lock(user,True)
    if not financial_writer(actor):raise PermissionDenied
    rental=visible(actor).select_for_update(of=('self',)).get(pk=pk)
    if not reason.strip():raise ValidationError('Укажите причину отмены.')
    row=rental.payments.select_for_update().get(pk=payment_id)
    if row.reversed:return row
    row.reversed=True;row.reversal_reason=reason;row.save(update_fields=['reversed','reversal_reason'])
    audit(actor,'finance_corrected',kind='rental_payment_reversed',payment=str(row.pk),reason=reason)


def totals(rental,on=None):
    days=rental.days.all();payments=rental.payments.filter(reversed=False)
    if on:days=days.filter(date__lte=on);payments=payments.filter(date__lte=on)
    charged=days.aggregate(v=Sum('amount'))['v'] or ZERO;paid=payments.aggregate(v=Sum('amount'))['v'] or ZERO
    return {'charged':charged,'paid':paid,'debt':max(ZERO,charged-paid),'advance':max(ZERO,paid-charged),'hours':days.aggregate(v=Sum('hours'))['v'] or 0,'unpriced':days.filter(hours__gt=0,rate__isnull=True).count()}


def missing_dates(rental,user=None,limit=367):
    start=rental.placement.start_date;end=min(rental.placement.end_date or timezone.localdate(),timezone.localdate())
    known=set(rental.days.values_list('date',flat=True));result=[]
    day=start
    while day<=end:
        if day not in known and (user is None or allowed_on(user,rental,day)):result.append(day)
        if len(result)>=limit:break
        day+=timedelta(days=1)
    return result

@transaction.atomic
def close_period(user,pk,start,end,note=''):
    if not financial_writer(user):raise PermissionDenied
    actor=actor_lock(user,True)
    if not financial_writer(actor):raise PermissionDenied
    rental=visible(actor).select_for_update(of=('self',)).get(pk=pk);period_dates(rental,start,end)
    if (end-start).days>366:raise ValidationError('Закройте период длиной не больше года.')
    if rental.periods.filter(starts_on__lte=end,ends_on__gte=start,reopened_at__isnull=True).exists():raise ValidationError('Этот период пересекается с уже закрытым.')
    expected=(end-start).days+1
    days=rental.days.filter(date__range=(start,end))
    if days.count()!=expected:raise ValidationError('В периоде есть дни без отметки. Отметьте 10, 4 или 0 часов перед закрытием.')
    if days.filter(hours__gt=0,rate__isnull=True).exists():raise ValidationError('Есть дни без тарифа. Сначала задайте стоимость аренды.')
    summary=totals(rental,end)
    snapshot={k:str(v) for k,v in summary.items()};snapshot['days']=[{'date':str(d.date),'version':d.version,'hours':d.hours,'amount':str(d.amount)} for d in days.order_by('date')]
    row=RentalPeriod.objects.create(rental=rental,starts_on=start,ends_on=end,snapshot=snapshot,note=note,created_by=actor)
    audit(actor,'finance_created',kind='rental_period_closed',period=str(row.pk));return row

@transaction.atomic
def reopen_period(user,pk,period_id,reason):
    if not financial_writer(user):raise PermissionDenied
    actor=actor_lock(user,True)
    if not financial_writer(actor):raise PermissionDenied
    rental=visible(actor).select_for_update(of=('self',)).get(pk=pk)
    if not reason.strip():raise ValidationError('Укажите причину повторной сверки.')
    row=rental.periods.select_for_update().get(pk=period_id)
    if row.reopened_at:return row
    row.reopened_at=timezone.now();row.reopened_by=actor;row.reopen_reason=reason
    row.save(update_fields=['reopened_at','reopened_by','reopen_reason'])
    audit(actor,'finance_corrected',kind='rental_period_reopened',period=str(row.pk),reason=reason)
    return row

@transaction.atomic
def link_finance(user,pk,scope):
    if not financial_writer(user):raise PermissionDenied
    actor=actor_lock(user,True)
    if not financial_writer(actor):raise PermissionDenied
    rental=visible(actor).select_for_update(of=('self',)).get(pk=pk)
    if scope and (scope.legal_entity.tax_id!=COMPANY_TAX_ID or scope.contract_id!=rental.placement.contract_id):raise ValidationError('Выберите финансовую карточку контракта этой техники.')
    if rental.financial_scope_id and rental.financial_scope_id!=(scope.pk if scope else None) and (rental.days.exists() or rental.payments.exists()):raise ValidationError('После начала начислений финансовая привязка сохраняется. Исправление требует отдельной сверки.')
    rental.financial_scope=scope;rental.version+=1;rental.save(update_fields=['financial_scope','version'])
    audit(actor,'finance_corrected',kind='rental_scope_link',rental=str(pk),scope=str(scope.pk if scope else ''))


def ensure_for_assignment(actor,placement):
    """Called by the equipment lifecycle under its lock, never by a read-only GET."""
    if placement.equipment.ownership!='rent':return None
    from finance.models import FinancialScope
    scopes=list(FinancialScope.objects.filter(contract_id=placement.contract_id)[:2]) if placement.contract_id else []
    row,created=EquipmentRental.objects.get_or_create(placement=placement,defaults={'supplier':placement.equipment.owner,'reference':'','financial_scope':scopes[0] if len(scopes)==1 else None,'created_by':actor})
    if created:audit(actor,'equipment_assigned',kind='rental_created_automatically',rental=str(row.pk),equipment=str(placement.equipment_id))
    return row

@transaction.atomic
def update_info(user,pk,supplier,reference,agreement,version):
    actor=actor_lock(user,True);row=visible(actor).select_for_update(of=('self',)).get(pk=pk)
    if row.version!=version:raise ValidationError('Условия уже изменены. Обновите карточку техники.')
    if agreement and (agreement.kind!='equipment_rent' or agreement.equipment_id not in (None,row.placement.equipment_id)):raise ValidationError('Договор относится к другой технике.')
    row.supplier=supplier.strip();row.reference=reference.strip();row.agreement=agreement;row.version+=1
    row.save(update_fields=['supplier','reference','agreement','version'])
    audit(actor,'equipment_assigned',kind='rental_conditions',rental=str(row.pk));return row
