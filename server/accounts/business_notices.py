"""Persistent per-recipient business inbox; viewing it never approves work."""
from django.db.models import Q
from django.urls import reverse
from .models import AuditEvent,BusinessNoticeRead,SupportRequest

NOISE=('login','logout','document_downloaded','document_exported','repair_exported','sessions_ended','mail_read','mail_prepared','mailbox_updated','agreement_updated')
def events(user):
    attention=Q(action__in=['repair_reported','work_reported','subcontract_reported','support_updated','document_integrity_error'])|Q(action='mail_sent',details__state__in=['failed','uncertain'])|Q(action='contract_updated',details__kind__in=['closure_request','customer_change_submitted'])
    qs=AuditEvent.objects.filter(attention)
    if not user.is_leader:
        # Employees receive replies to their own support tickets only.
        ids=SupportRequest.objects.filter(author=user).values_list('pk',flat=True)
        qs=qs.filter(action='support_updated',details__reply=True,details__request_id__in=list(ids))
    return qs.select_related('actor').order_by('-created_at','-pk')

def unread(user):
    return events(user).exclude(pk__in=BusinessNoticeRead.objects.filter(user=user).values('event_id'))

def destination(event):
    if event.action=='contract_updated' and event.details.get('kind')=='closure_request':return reverse('contract_closure',args=[event.details['entity_id']])
    if event.action=='contract_updated' and event.details.get('kind')=='customer_change_submitted':return reverse('contract_changes',args=[event.details['entity_id']])
    if event.action.startswith('work_'):return reverse('daily_detail',args=[event.details['work']])
    if event.action.startswith(('mail_','mailbox_')):return reverse('mail')
    if event.action=='support_updated':return reverse('support_detail',args=[event.details['request_id']])
    if event.action.startswith('repair_'):return reverse('repairs')+'?state=reported' if event.action=='repair_reported' else reverse('work_home')
    if event.action.startswith('subcontract_'):return reverse('subcontracts')
    if event.action.startswith('equipment_'):return reverse('equipment')
    if event.action.startswith('stock_'):return reverse('stock')
    if event.action.startswith('finance_'):return reverse('finance')
    if event.action.startswith('document_'):return reverse('documents')
    if event.action.startswith('person_'):return reverse('people')
    return reverse('audit')

def notice_title(event):
    if event.action=='mail_sent':return 'Неясный результат отправки письма' if event.details.get('state')=='uncertain' else 'Ошибка отправки письма'
    if event.action=='contract_updated':return 'Запрос на закрытие контракта' if event.details.get('kind')=='closure_request' else 'Указание заказчика на проверке'
    if event.action=='support_updated':return 'Обращение № '+str(event.details.get('request_id',''))
    return {'repair_reported':'Ремонтные работы на проверке','work_reported':'Ежедневные работы на проверке','subcontract_reported':'Отчёт субподрядчика на проверке','document_integrity_error':'Ошибка сохранности документа'}.get(event.action,event.get_action_display())
