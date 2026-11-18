"""Native finance input and explicit confirmation, using the LC services/forms."""
import hashlib
import json
import uuid
from datetime import timedelta
from django import forms
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from projects import rentals, rental_settlements as ledger, rental_views as views

ACTIONS={
    'range':('Расчёт за выбранные даты',views.RangeForm,None),
    'unpay':('Отменить ошибочный платёж',views.ReversePaymentForm,rentals.reverse_payment),
    'payment':('Перечисление за услуги / залог',views.PaymentForm,rentals.payment),
    'classify':('Разнести прежний платёж',views.ClassifyPaymentForm,rentals.classify_payment),
    'deposit':('Возврат / зачёт залога',views.DepositUseForm,rentals.use_deposit),
    'invoice':('Сверить счёт за период',views.InvoiceForm,ledger.create_invoice),
    'allocate':('Привязать оплату к счёту',views.AllocationForm,ledger.allocate),
    'unallocate':('Отменить привязку оплаты',views.ReverseAllocationForm,ledger.reverse_allocation),
    'void':('Отменить сверку счёта',views.VoidInvoiceForm,ledger.void_invoice),
    'undeposit':('Отменить операцию с залогом',views.ReverseDepositForm,rentals.reverse_deposit_use),
}


def accessible(user):
    from .leadership import rentals as visible
    return visible(user)


def delivery_allowed(user,scope):
    return bool(rentals.financial_reader(user) and accessible(user).filter(pk=scope['rental']).exists()
        and (not scope.get('write') or rentals.financial_writer(user)))


def fingerprint(rental):
    data=[rental.version]
    for rows in [rental.days.order_by('pk'),rental.payments.order_by('pk'),rental.periods.order_by('pk'),
        rentals.RentalDepositUse.objects.filter(payment__rental=rental).order_by('pk'),
        ledger.RentalInvoice.objects.filter(period__rental=rental).order_by('pk'),
        ledger.RentalPaymentAllocation.objects.filter(payment__rental=rental).order_by('pk')]:
        data.append(list(rows.values()))
    return hashlib.sha256(json.dumps(data,sort_keys=True,default=str).encode()).hexdigest()


def emit(event,binding,rental,text,buttons=(),write=False):
    from .dialog import queue
    queue(event,str(rental.placement.equipment)+'\n'+text,buttons,binding=binding,
        scope={'rental_finance':{'rental':str(rental.pk),'write':write}})


def save(binding,draft):
    binding.menu_scope={**binding.menu_scope,'rental_finance':draft}
    binding.save(update_fields=['menu_scope'])


def prompt(event,binding,rental,draft,page=0):
    from .dialog import button
    form=ACTIONS[draft['action']][1]({'_money_unit':'rubles'})
    name=draft['fields'][draft['step']];field=form.fields[name]
    buttons=[]
    if isinstance(field,forms.ChoiceField):
        buttons=[button(label,'rentfin:value:'+str(value)) for value,label in field.choices]
    pickers={'payment_id':rental.payments.filter(reversed=False).order_by('-pk'),
        'invoice_id':ledger.RentalInvoice.objects.filter(period__rental=rental,void_reason='').order_by('-pk'),
        'operation_id':rentals.RentalDepositUse.objects.filter(payment__rental=rental,reversed=False).order_by('-pk'),
        'allocation_id':ledger.RentalPaymentAllocation.objects.filter(payment__rental=rental,reversed=False).order_by('-pk')}
    if name in pickers:
        if not 0<=page<=10000:raise ValidationError('Неверная страница выбора.')
        rows=list(pickers[name][page*4:page*4+5])
        from finance.templatetags.finance_format import rub
        for row in rows[:4]:
            label=f'№{row.pk} · '+(row.reference if name=='invoice_id' else f'{row.date:%d.%m.%Y} · {rub(row.amount)}')
            buttons.append(button(label,'rentfin:value:'+str(row.pk)))
        if page:buttons.append(button('Предыдущие записи',f'rentfin:choices:{page-1}'))
        if len(rows)>4:buttons.append(button('Следующие записи',f'rentfin:choices:{page+1}'))

    buttons.append(button('Отменить','rentfin:cancel'))
    label=str(field.label)
    emit(event,binding,rental,label+'\n'+('Формат даты: ГГГГ-ММ-ДД.' if isinstance(field,forms.DateField) else 'Суммы вводятся в рублях.' if isinstance(field,forms.DecimalField) else '')+(' Можно отправить «-», чтобы пропустить.' if not field.required else ''),buttons,write=draft['action']!='range')


def card(event,binding,rental,page=0):
    from .dialog import button
    from finance.templatetags.finance_format import rub
    if not 0<=page<=10000:raise ValidationError('Неверная страница.')
    totals=rentals.totals(rental);info=ledger.summary(rental)
    historical=rentals.equipment_cost(binding.user,rental.placement.equipment_id)
    body=(f'Начислено по выбранной аренде: {rub(totals["charged"])}\nУчтено за услуги: {rub(totals["paid"])}\nОстаток к оплате: {rub(totals["debt"])}\nЗалог: {rub(totals["deposit"])}\nС {info["next_start"]:%d.%m.%Y}: {info["new_hours"]} ч, {rub(info["new_charged"])}\nДолг по сверенным счетам: {rub(info["invoice_debt"])}\nНепривязанная оплата: {rub(info["unallocated"])}\nНачисления и долг по счетам не складываются.')
    body+=f'\nСтоимость аренды техники по всем доступным назначениям: {rub(historical["charged"])} · {historical["hours"]} ч.'
    if totals['unpriced'] or rentals.missing_dates(rental):body+='\nРасчёт неполный: есть дни без отметки или тарифа.'
    if totals['unconfirmed_payments']:body+='\nУ прежних платежей нужно подтвердить назначения.'
    rows=list(rental.payments.all()[page*5:page*5+6])
    for p in rows[:5]:body+=f'\nПлатёж №{p.pk} от {p.date:%d.%m.%Y}: {rub(p.amount)}, залог {rub(p.deposit_amount)}'+(' · отменён' if p.reversed else '')
    for inv,balance in info['invoices'][page*5:page*5+5]:
        body+=f'\nСверка №{inv.pk} · {inv.period.starts_on:%d.%m}–{inv.period.ends_on:%d.%m.%Y}: {rub(balance["charged"])}, долг {rub(balance["debt"])}'+(' · расхождение с документом' if balance['difference'] else '')
    buttons=[button('Расчёт за выбранные даты',f'rentfin:start:{rental.pk}:range'),button('Счета, акты и расчёты',f'rentfile:invoices:{rental.pk}:0')]
    for operation in rentals.RentalDepositUse.objects.filter(payment__rental=rental).order_by('-pk')[page*5:page*5+5]:
        body+=f'\nОперация залога №{operation.pk}: {operation.get_kind_display()}, {rub(operation.amount)}'+(' · отменена' if operation.reversed else '')
    for allocation in ledger.RentalPaymentAllocation.objects.filter(payment__rental=rental).order_by('-pk')[page*5:page*5+5]:
        body+=f'\nПривязка №{allocation.pk}: платёж {allocation.payment_id} → сверка {allocation.invoice_id}, {rub(allocation.amount)}'+(' · отменена' if allocation.reversed else '')
    if rentals.financial_writer(binding.user):
        buttons += [button(title,f'rentfin:start:{rental.pk}:{action}') for action,(title,_,_) in ACTIONS.items() if action!='range']
    if page:buttons.append(button('Предыдущая история',f'rentfin:home:{rental.pk}:{page-1}'))
    if len(rows)>5 or len(info['invoices'])>(page+1)*5 or rentals.RentalDepositUse.objects.filter(payment__rental=rental).count()>(page+1)*5 or ledger.RentalPaymentAllocation.objects.filter(payment__rental=rental).count()>(page+1)*5:buttons.append(button('Следующая история',f'rentfin:home:{rental.pk}:{page+1}'))
    emit(event,binding,rental,body,buttons)


def _handle(event,binding):
    from .dialog import button
    text=event.payload.get('text','').strip()
    draft=binding.menu_scope.get('rental_finance')
    if not text.startswith('rentfin:') and (not draft or draft['state'] not in ('editing','confirm') or (text.startswith('/') and text!='/отмена')):return False
    parts=text.split(':')
    if text.startswith('rentfin:home:'):
        rental=accessible(binding.user).get(pk=parts[2]);card(event,binding,rental,int(parts[3]));return True
    if text.startswith('rentfin:start:'):
        rental=accessible(binding.user).get(pk=parts[2])
        action=parts[3]
        if action!='range' and not rentals.financial_writer(binding.user):raise PermissionDenied
        if action not in ACTIONS:raise ValidationError('Неизвестное действие.')
        form=ACTIONS[action][1]({'_money_unit':'rubles'})
        fields=[name for name,field in form.fields.items() if not field.widget.is_hidden]
        # A payment id is user input here; LC embeds it in its payment row.
        if action=='classify':fields=['payment_id','deposit_amount','reason']
        if action in ('unallocate','void','undeposit'):
            fields=[{'unallocate':'allocation_id','void':'invoice_id','undeposit':'operation_id'}[action],'reason']
        draft={'action':action,'rental':str(rental.pk),'fields':fields,'step':0,'data':{'token':str(uuid.uuid4()),'_money_unit':'rubles'},'state':'editing','expires':(timezone.now()+timedelta(minutes=30)).isoformat()}
        binding.menu_scope.pop('rental_file',None)
        save(binding,draft);prompt(event,binding,rental,draft);return True
    if not draft:raise ValidationError('Откройте расчёты аренды заново.')
    rental=accessible(binding.user).get(pk=draft['rental'])
    if draft['action']!='range' and not rentals.financial_writer(binding.user):raise PermissionDenied
    if text in ('rentfin:cancel','/отмена'):
        draft['state']='cancelled';save(binding,draft)
        emit(event,binding,rental,'Действие отменено.',[button('Расчёты аренды',f'rentfin:home:{rental.pk}:0')]);return True
    if timezone.now()>timezone.datetime.fromisoformat(draft['expires']):raise ValidationError('Время подтверждения истекло. Начните действие заново.')
    if text=='rentfin:confirm':
        if draft['state']=='submitted':
            emit(event,binding,rental,'Действие уже сохранено. Повторная запись не создана.');return True
        if draft['state']!='confirm':raise ValidationError('Сначала заполните данные.')
        actor,rental=rentals.financial_lock(binding.user,rental.pk)
        if fingerprint(rental)!=draft['fingerprint']:raise ValidationError('Данные аренды изменились. Начните действие заново для новой сверки.')
        form=ACTIONS[draft['action']][1](draft['data'])
        if not form.is_valid():raise ValidationError('Проверьте введённые данные.')
        params={k:v for k,v in form.cleaned_data.items() if not k.startswith('_')}
        ACTIONS[draft['action']][2](actor,rental.pk,**params)
        draft['state']='submitted';save(binding,draft)
        emit(event,binding,rental,'Сохранено.',[button('Расчёты аренды',f'rentfin:home:{rental.pk}:0')]);return True
    if draft['state']!='editing':raise ValidationError('Нажмите «Подтвердить» или «Отменить».')
    if text.startswith('rentfin:choices:'):
        prompt(event,binding,rental,draft,int(parts[2]));return True
    if event.payload.get('photos'):raise ValidationError('На этом шаге нужен текст. Файлы здесь не сохраняются.')
    name=draft['fields'][draft['step']];form=ACTIONS[draft['action']][1]({'_money_unit':'rubles'})
    field=form.fields[name]
    raw=parts[2] if text.startswith('rentfin:value:') and len(parts)==3 else text
    if raw=='-' and not field.required:raw='0' if isinstance(field,forms.DecimalField) else ''
    if isinstance(field,forms.DecimalField):raw=raw.replace(',','.')
    field.clean(raw)
    draft['data'][name]=raw
    if draft['action']=='classify' and name=='payment_id':
        payment=rental.payments.get(pk=raw);draft['data']['version']=payment.purpose_version
    draft['step']+=1
    if draft['step']<len(draft['fields']):
        save(binding,draft);prompt(event,binding,rental,draft);return True
    form=ACTIONS[draft['action']][1](draft['data'])
    if not form.is_valid():raise ValidationError('Проверьте данные: '+str(form.errors.as_text()))
    if draft['action']=='range':
        result=rentals.period_totals(rental,**form.cleaned_data)
        from finance.templatetags.finance_format import rub
        draft['state']='submitted';save(binding,draft)
        emit(event,binding,rental,f'{result["start"]:%d.%m.%Y} — {result["end"]:%d.%m.%Y}: {result["hours"]} ч, {rub(result["charged"])}\nДней без отметки: {result["missing"]}; без тарифа: {result["unpriced"]}.')
        return True
    draft['state']='confirm';draft['fingerprint']=fingerprint(rental);save(binding,draft)
    body='Проверьте: '+ACTIONS[draft['action']][0]
    for key in draft['fields']:body+='\n'+str(form.fields[key].label)+': '+str(draft['data'][key])
    emit(event,binding,rental,body+'\nИзменения сохранятся после подтверждения.',[button('Подтвердить','rentfin:confirm'),button('Отменить','rentfin:cancel')],write=True)
    return True


@transaction.atomic
def handle(event,binding):
    try:return _handle(event,binding)
    except ObjectDoesNotExist:raise PermissionDenied from None
    except (ValueError,IndexError,KeyError):raise ValidationError('Откройте расчёты аренды заново.') from None
