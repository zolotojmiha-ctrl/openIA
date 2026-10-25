"""Persistent per-recipient business inbox; viewing it never approves work."""
from django.db.models import Q
from django.urls import reverse
from .models import AuditEvent,BusinessNoticeRead,SupportRequest

NOISE=('login','logout','document_downloaded','document_exported','repair_exported','sessions_ended')
def events(user):
    qs=AuditEvent.objects.exclude(action__in=NOISE)
    if not user.is_leader:
        # Employees receive replies to their own support tickets only.
        ids=SupportRequest.objects.filter(author=user).values_list('pk',flat=True)
        qs=qs.filter(action='support_updated',details__reply=True,details__request_id__in=list(ids))
    return qs.select_related('actor').order_by('-created_at','-pk')

def unread(user):
    return events(user).exclude(pk__in=BusinessNoticeRead.objects.filter(user=user).values('event_id'))

def destination(event):
    if event.action=='support_updated':return reverse('support_detail',args=[event.details['request_id']])
    if event.action.startswith('repair_'):return reverse('repairs')+'?state=reported' if event.action=='repair_reported' else reverse('work_home')
    if event.action.startswith('subcontract_'):return reverse('subcontracts')
    if event.action.startswith('equipment_'):return reverse('equipment')
    if event.action.startswith('stock_'):return reverse('stock')
    if event.action.startswith('finance_'):return reverse('finance')
    if event.action.startswith('document_'):return reverse('documents')
    if event.action.startswith('person_'):return reverse('people')
    return reverse('audit')
