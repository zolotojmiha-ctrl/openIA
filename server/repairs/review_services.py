"""Leader decisions are separate from the foreman's original evidence."""
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction
from django.utils import timezone
from accounts.models import User
from accounts.views import audit
from .access import visible_jobs
from .models import Job,Report,Attachment
from .services import confirm_work,report_work
from documents.storage import private_path

def leader(user):
    actor=User.objects.select_for_update().get(pk=user.pk)
    if not actor.is_active or not actor.is_leader or actor.session_version!=user.session_version:raise PermissionDenied
    return actor

def latest_result(job):return job.reports.filter(kind__in=['complete','partial','correction']).first()

@transaction.atomic
def confirm_many(user,snapshot):
    actor=leader(user)
    if not snapshot or len(snapshot)>10000:raise ValidationError('Выберите от 1 до 10000 ремонтов. Для большего списка уточните фильтр.')
    ids=list(snapshot)
    jobs=list(visible_jobs(actor).select_for_update(no_key=True,of=('self',)).filter(pk__in=ids).order_by('pk'))
    if len(jobs)!=len(ids):raise ValidationError('Состав работ или доступ изменился. Проверьте список заново.')
    for job in jobs:
        if job.version!=snapshot[str(job.pk)] or job.state!='reported':raise ValidationError('Одна из работ уже изменена или проверена. Ничего не подтверждено; обновите выбор.')
        result=latest_result(job)
        if not result or result.outcome!='complete':raise ValidationError('В выборе есть незавершённый результат. Ничего не подтверждено.')
    for job in jobs:confirm_work(actor,job.pk,job.version)
    audit(actor,'repair_confirmed',kind='batch',jobs=[str(j.pk) for j in jobs],count=len(jobs))
    return len(jobs)

@transaction.atomic
def return_for_revision(user,job_id,version,note):
    actor=leader(user);note=note.strip()
    if not note or len(note)>3000:raise ValidationError('Укажите пояснение до 3000 символов.')
    job=visible_jobs(actor).select_for_update(no_key=True,of=('self',)).get(pk=job_id)
    result=latest_result(job)
    if job.version!=version or job.state!='reported' or not result or result.outcome!='complete':raise ValidationError('Результат уже изменён. Проверьте актуальную карточку.')
    Report.objects.create(job=job,author=actor,kind='revision',performed_date=timezone.localdate(),note=note,context={'returned_report':str(result.pk),'previous_version':job.version})
    job.review_note=note;job.state='progress';job.version+=1;job.save(update_fields=['review_note','state','version'])
    audit(actor,'repair_corrected',entity_id=str(job.pk),kind='return_for_revision',report=str(result.pk),reason=note)
    return job

def correct_and_accept(user,job_id,data):
    new_files=[]
    try:
        with transaction.atomic():
            actor=leader(user)
            from projects.stock import lock as stock_lock
            stock_lock()
            # report_work validates retry token/digest, version, scope and physical couplings.
            existed=Report.objects.filter(token=data['token']).exists()
            job=visible_jobs(actor).select_for_update(no_key=True,of=('self',)).get(pk=job_id)
            previous=latest_result(job)
            if not existed and job.state!='reported':raise ValidationError('Исправить и принять можно только результат на проверке.')
            if data.get('outcome')!='complete':raise ValidationError('Для принятия нужен завершённый результат.')
            report=report_work(actor,job_id,'correction',data)
            if not existed:
                new_files=list(report.attachments.values_list('storage_key',flat=True))
                report.context={**report.context,'corrected_report':str(previous.pk) if previous else None,'review_action':'correct_and_accept'};report.save(update_fields=['context'])
            if report.confirmed_at:return job
            job.refresh_from_db();confirm_work(actor,job.pk,job.version)
            return job
    except Exception:
        for key in new_files:private_path(key).unlink(missing_ok=True)
        raise


@transaction.atomic
def decide_problem(user,job_id,version,decision,note):
    actor=leader(user)
    from projects import stock,stock_planning
    from projects.models import Contract,StockItem
    stock.lock()
    scope=visible_jobs(actor).get(pk=job_id)
    if scope.contract_id:Contract.objects.select_for_update(no_key=True).get(pk=scope.contract_id)
    job=visible_jobs(actor).select_for_update(no_key=True,of=('self',)).get(pk=job_id)
    if job.version!=version:raise ValidationError('Карточка изменилась. Проверьте актуальное сообщение.')
    if job.contract_id and job.contract.status=='archived':raise ValidationError('Контракт в архиве — только просмотр.')
    note=note.strip()
    if not note or len(note)>3000:raise ValidationError('Укажите основание решения, до 3000 символов.')
    if decision=='reopen':
        if job.state!='unrepairable':raise ValidationError('Этот дефект не закрыт как неустранимый.')
        Report.objects.create(job=job,author=actor,kind='reopen',performed_date=timezone.localdate(),note=note,context={'previous_reason':job.problem_note})
        job.state='progress';job.problem='';job.problem_note=''
    else:
        if decision not in ('acknowledge','unrepairable') or not job.problem or job.state in ('reported','confirmed','unrepairable'):raise ValidationError('Нет актуального препятствия для рассмотрения.')
        report=job.reports.filter(kind='problem').first()
        if not report or report.confirmed_at:raise ValidationError('Сообщение уже рассмотрено или отсутствует. Обновите карточку.')
        released=[]
        if decision=='unrepairable':
            for item in StockItem.objects.filter(reserved_job=job,deleted_at__isnull=True).exclude(coupling__installation__isnull=False):
                stock_planning.reserve(actor,item.pk,item.version,None);released.append(str(item.pk))
            job.state='unrepairable';job.problem_note=note[:1000]
        Report.objects.create(job=job,author=actor,kind='problem_review',performed_date=timezone.localdate(),note=note,problem=job.problem,context={'decision':decision,'problem_report':str(report.pk),'released_stock':released})
        report.confirmed_by=actor;report.confirmed_at=timezone.now();report.save(update_fields=['confirmed_by','confirmed_at'])
        from accounts.business_notices import mark_repair_read
        mark_repair_read(actor,job,report)
    job.version+=1;job.save(update_fields=['state','problem','problem_note','version'])
    audit(actor,'repair_corrected',entity_id=str(job.pk),kind='problem_review',decision=decision,reason=note)
    return job
