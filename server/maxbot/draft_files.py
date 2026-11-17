from documents.storage import discard_file
"""Discard temporary copies only after the report transaction has committed."""
from django.db import transaction
from documents.storage import private_path
from repairs.models import Attachment
from .models import Photo


def release(draft):
    keys=list(draft.photos.values_list('storage_key',flat=True))
    draft.photos.all().delete()
    def cleanup():
        for key in keys:
            # Legacy/imported shared keys must never damage submitted evidence.
            if not Attachment.objects.filter(storage_key=key).exists() and not Photo.objects.filter(storage_key=key).exists():
                discard_file(key)
    transaction.on_commit(cleanup)


def report_button(report):
    from django.conf import settings
    from django.urls import reverse
    return {'type':'link','text':'Открыть сохранённый отчёт','url':settings.ARMADA_MAX_PUBLIC_ORIGIN.rstrip('/')+reverse('repair_detail',args=[report.job_id])+f'?report={report.pk}#report-{report.pk}'}
