from django.conf import settings

def local_testing(request):
    context={'lan_test':settings.LAN_TEST,'release_version':'6.0.3-repairs'}
    if request.user.is_authenticated:
        from repairs.access import visible_jobs,can_prepare
        jobs=visible_jobs(request.user)
        context.update(can_repairs=can_prepare(request.user) or jobs.exists(),can_prepare_repairs=can_prepare(request.user),repair_job_count=jobs.count(),repair_review_count=jobs.filter(state='reported').count() if request.user.is_leader else 0)
        from .models import Preference
        from documents.access import unread_documents,unsorted_documents,visible_documents
        from documents.models import Notice
        preference,_=Preference.objects.get_or_create(user=request.user)
        from documents.access import has_permission
        context.update(can_upload=has_permission(request.user,'document.upload'),can_finance=has_permission(request.user,'finance.read') or has_permission(request.user,'finance.write'),preferences=preference,inbox_count=unread_documents(request.user).count(),unsorted_count=unsorted_documents(request.user).count(),comment_notice_count=Notice.objects.filter(user=request.user,read=False,document__in=visible_documents(request.user)).count())
    return context
