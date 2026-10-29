from django.conf import settings

def local_testing(request):
    from .presets import PRESETS
    context={'permission_presets':PRESETS,'lan_test':settings.LAN_TEST,'release_version':'6.21.0'}
    if request.user.is_authenticated and request.user.role=='customer' and not request.user.is_leader:return context
    if request.user.is_authenticated:
        from .permissions import permitted,can_link_person
        from projects.access import visible_contracts
        context.update(can_manage_users=permitted(request.user,'users.manage'),can_manage_projects=permitted(request.user,'projects.manage'),can_manage_project_access=permitted(request.user,'projects.access'),can_manage_personnel=permitted(request.user,'personnel.manage'),can_link_accounts=can_link_person(request.user),can_customer_changes=permitted(request.user,'projects.manage') or permitted(request.user,'contract.changes'),can_work=visible_contracts(request.user).exists())
        from repairs.access import visible_jobs,can_prepare
        jobs=visible_jobs(request.user)
        context.update(can_repairs=can_prepare(request.user) or jobs.exists(),can_prepare_repairs=can_prepare(request.user),repair_job_count=jobs.count(),repair_review_count=jobs.filter(state='reported').count() if request.user.is_leader else 0)
        context['can_work']=context['can_work'] or can_prepare(request.user) or jobs.exists() or context['can_manage_projects']
        if request.user.is_leader:
            from projects.daily import works as daily_works
            context['daily_review_count']=daily_works(request.user).filter(state='reported').count()
            from projects.closure import pending_contracts
            context['contract_closure_count']=pending_contracts(request.user).count()
        context['repair_return_count']=jobs.filter(assigned_to=request.user).exclude(review_note='').count()
        from projects.subcontracts import companies,orders,returned_orders
        context.update(can_manage_subcontracts=permitted(request.user,'subcontract.manage'),can_subcontracts=permitted(request.user,'subcontract.manage') or companies(request.user).exists(),subcontract_review_count=orders(request.user).filter(state='reported').count() if request.user.is_leader else 0)
        context['subcontract_return_count']=returned_orders(request.user).count() if not request.user.is_leader else 0
        from projects.stock import warehouses
        context.update(can_manage_stock=permitted(request.user,'stock.manage'),can_stock=permitted(request.user,'stock.manage') or warehouses(request.user).exists())
        from projects.equipment import visible_equipment
        context.update(can_manage_equipment=permitted(request.user,'equipment.manage'),can_equipment=permitted(request.user,'equipment.manage') or visible_equipment(request.user).exists())
        from documents.deadlines import due_documents as due_files
        context['document_due_count']=len(due_files(request.user))
        from .business_notices import unread as business_unread
        context['business_notice_count']=business_unread(request.user).count()
        from postoffice.access import boxes
        context['mail_count']=sum(boxes(request.user).filter(state='active').values_list('unread',flat=True))
        from .models import Preference
        from documents.access import unread_documents,unsorted_documents,visible_documents
        from documents.models import Notice
        preference,_=Preference.objects.get_or_create(user=request.user)
        from documents.access import has_permission
        context.update(can_upload=has_permission(request.user,'document.upload'),can_finance=has_permission(request.user,'finance.read') or has_permission(request.user,'finance.write'),preferences=preference,inbox_count=unread_documents(request.user).count(),unsorted_count=unsorted_documents(request.user).count(),comment_notice_count=Notice.objects.filter(user=request.user,read=False,document__in=visible_documents(request.user)).count())
    return context
