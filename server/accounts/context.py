from django.conf import settings

def local_testing(request):
    context={'lan_test':settings.LAN_TEST,'release_version':'4.0.0-workspace'}
    if request.user.is_authenticated:
        from .models import Preference
        from documents.access import unread_documents,unsorted_documents,visible_documents
        from documents.models import Notice
        preference,_=Preference.objects.get_or_create(user=request.user)
        from documents.access import has_permission
        context.update(can_upload=has_permission(request.user,'document.upload'),can_finance=has_permission(request.user,'finance.read') or has_permission(request.user,'finance.write'),preferences=preference,inbox_count=unread_documents(request.user).count(),unsorted_count=unsorted_documents(request.user).count(),comment_notice_count=Notice.objects.filter(user=request.user,read=False,document__in=visible_documents(request.user)).count())
    return context
