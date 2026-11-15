"""Concrete installation ledger events, never inferred from calculated shortage."""
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from accounts.permissions import permitted
from projects import stock
from projects.diameters import label as diameter_label
from repairs.access import visible_jobs
from repairs.models import Report
from .leadership import supported, title, link
from .links import current
from .models import Binding, Outbox


def data(user, report_id):
    if not supported(user) or not permitted(user, 'stock.manage'):
        return None
    report = Report.objects.select_related('job__contract', 'job__section__project_object').filter(pk=report_id).first()
    if not report or not visible_jobs(user).exclude(contract__status='archived').exclude(
            section__project_object__status='archived').filter(pk=report.job_id).exists():
        return None
    # An unchanged installation keeps its original posting even after a correction.
    # Only a real reversal invalidates the queued consumption notice.
    rows = list(stock.movements(user).filter(report=report, automatic=True, item__kind='coupling',
        item__coupling__isnull=False).filter(Q(kind='consume', reversal__isnull=True) |
        Q(kind='reversal', reverses__kind='consume')).select_related('reverses__report').order_by('created_at', 'pk'))
    return (report, rows) if rows else None


def item_text(row, report):
    source_report = row.reverses.report if row.reverses_id else report
    saved = next((s for s in source_report.coupling_snapshot
                  if s['id'] == str(row.item.coupling_id)), {}) if source_report else {}
    item = row.item
    serial = saved.get('serial') or item.serial or 'не указан'
    method = saved.get('method') or item.method_label
    length = saved.get('length_mm') or item.length_mm or '—'
    wall = saved.get('wall_mm') or item.wall_mm or '—'
    warehouse = row.destination if row.kind == 'reversal' else row.source
    verb = 'Возвращена на склад' if row.kind == 'reversal' else 'Списана со склада'
    return (f'{verb}: {warehouse.name if warehouse else "не указан"}\n'
            f'{method} · {diameter_label(item.diameter_mm)} · {length} мм · стенка {wall} мм\n'
            f'Заводской №: {serial}\nКод: {item.sku} · {row.quantity.normalize():f} шт.\n'
            f'Дата операции: {row.date:%d.%m.%Y}')


def queue_event(event):
    if not settings.ARMADA_MAX_ENABLED or event.action != 'repair_reported':
        return
    report_id = event.details.get('report_id')
    if not report_id:
        return
    recipients = Binding.objects.filter(active=True).filter(
        Q(user__leader_slot__in=[1, 2]) | Q(user__role='pto')).select_related('user')
    for binding in recipients:
        if not current(binding):
            continue
        result = data(binding.user, report_id)
        if not result:
            continue
        report, rows = result
        # One concrete movement per message: long installations never lose their tail.
        for row in rows:
            heading = 'Муфта возвращена после исправления отчёта' if row.kind == 'reversal' else 'Муфта списана на ремонт'
            context = title(binding.user, report.job)
            if len(context) > 2200:
                context = context[:2199] + '…'
            text = heading + '\n' + context + '\n\n' + item_text(row, report)
            Outbox.objects.get_or_create(key=f'coupling-stock:{row.pk}:{binding.pk}', defaults={
                'binding': binding, 'job': report.job, 'max_user': binding.max_user, 'chat': binding.chat,
                'body': {'text': text, 'attachments': [{'type': 'inline_keyboard', 'payload': {
                    'buttons': [[link('Открыть отчёт в ЛК', 'repair_detail', [report.job_id])]]}}]},
                'scope': {'coupling_stock': {'report': str(report.pk), 'movement': str(row.pk)}},
                'available_at': timezone.now(), 'expires': timezone.now() + timedelta(days=1)})


def delivery_allowed(user, scope):
    result = data(user, scope['report'])
    return bool(result and any(str(row.pk) == scope['movement'] for row in result[1]))
