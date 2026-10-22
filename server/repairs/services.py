import hashlib
import json
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction
from django.utils import timezone
from accounts.models import User
from accounts.views import audit
from documents.storage import store_file, private_path
from .access import visible_jobs, reportable_jobs
from .models import Job, Report, Coupling, CouplingUse, Attachment

def report_work(user,job_id,action,data):
    """Append-only evidence, optimistic conflict detection and retry idempotence."""
    not_repaired=action=='unrepaired'
    if not_repaired:action='problem'
    stored=[]
    try:
        with transaction.atomic():
            current=User.objects.select_for_update().get(pk=user.pk)
            if not current.is_active or current.session_version!=user.session_version:raise ValidationError('Доступ изменился. Войдите заново.')
            if action in ('result','correction') or data.get('couplings'):
                from projects.stock import lock as stock_lock
                stock_lock()
            if current.role=='subcontractor':
                scope=reportable_jobs(current).filter(pk=job_id).first()
                if not scope:raise PermissionDenied
                if scope.subcontract_order_id:
                    from projects.subcontracts import lock_external
                    lock_external(current,scope.subcontract_order_id,True)
            job=reportable_jobs(current).select_for_update(no_key=True,of=('self',)).filter(pk=job_id).first()
            if not job:raise PermissionDenied
            digest=hashlib.sha256(json.dumps({'action':'unrepaired' if not_repaired else action,**{key:str(data.get(key) or '') for key in ['performed_date','method','length_m','composition','note','problem','latitude','longitude','outcome']},'couplings':sorted(str(c.pk) for c in data.get('couplings',[])),'files':[{k:f[k] for k in ['name','sha256']} for f in data.get('files',[])]},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            previous=Report.objects.filter(token=data['token']).first()
            if previous:
                if previous.job_id==job.pk and previous.author_id==user.pk:
                    if previous.request_digest==digest:return previous
                    raise ValidationError('Предыдущая отправка уже получена сервером, но этот черновик отличается. Можно сохранить изменения отдельной записью после проверки карточки.',code='already_saved',params={'version':job.version})
                raise ValidationError('Этот запрос уже использован для другой записи.')
            if job.version!=data['expected_version']:raise ValidationError('Работу уже изменили. Ваш черновик сохранён. Откройте актуальную карточку, сравните данные и повторите отправку.')
            if action=='correction' and not current.is_leader:raise ValidationError('Исправление доступно руководителю.')
            if job.state=='confirmed' and action not in ('note','correction') and not (action=='resolve' and current.is_leader):raise ValidationError('Результат уже подтверждён. Исправление вносит руководитель с указанием причины.')
            if action=='resolve' and not job.problem:raise ValidationError('Открытого препятствия уже нет. Обновите карточку.')
            if action=='start' and job.state not in ('planned','progress'):raise ValidationError('Результат уже внесён. Откройте карточку.')
            selected=list(data.get('couplings',[]))
            if current.role=='subcontractor' and selected:
                from projects.stock import prepared_couplings
                allowed={c.pk for c in prepared_couplings(job)}|set(CouplingUse.objects.filter(job=job).values_list('coupling_id',flat=True))
                if any(c.pk not in allowed for c in selected):raise PermissionDenied
            old_ids=list(CouplingUse.objects.filter(job=job).values_list('coupling_id',flat=True)) if action in ('result','correction') else []
            locked=list(Coupling.objects.select_for_update().filter(pk__in=set([c.pk for c in selected]+old_ids)).order_by('pk'))
            couplings=[c for c in locked if c.pk in {s.pk for s in selected} and c.project_object_id==job.section.project_object_id]
            if len(couplings)!=len(selected):raise ValidationError('Состав муфт изменился. Обновите страницу.')
            if CouplingUse.objects.filter(coupling__in=couplings).exclude(job=job).exists():raise ValidationError('Одна из муфт уже установлена на другой работе. Выберите актуальные изделия.')
            if not current.is_leader and Coupling.objects.filter(pk__in=[c.pk for c in couplings],source__historical_installation=True).exclude(installation__job=job).exists():raise ValidationError('В ведомости указан прежний монтаж одной из муфт. Руководитель должен проверить её связь с этой работой.')
            result=action in ('result','correction')
            kind=data.get('outcome','') if action=='result' else action
            report=Report.objects.create(job=job,author=current,token=data['token'],request_digest=digest,kind=kind,performed_date=data['performed_date'],
                method=data.get('method',''),length_m=data.get('length_m'),composition=data.get('composition',''),note=data.get('note',''),problem=data.get('problem',''),
                latitude=data.get('latitude'),longitude=data.get('longitude'),outcome=data.get('outcome',''),
                coupling_snapshot=[{'id':str(c.pk),'serial':c.serial,'method':c.get_method_display(),'length_mm':c.length_mm,'warehouse':c.warehouse} for c in couplings],
                context={'subcontract':{'order':str(job.subcontract_order_id),'company':str(job.subcontract_order.company_id),'name':job.subcontract_order.company.name} if job.subcontract_order_id else None,'defects':[{'number':d.number,'km':d.km,'section':d.pipe_section} for d in job.active_defects],'crew':{'assignment':str(job.crew_assignment_id),'name':job.crew_assignment.brigade.name,'roster':job.crew_assignment.roster} if job.crew_assignment_id else None,'planned_method':job.method,'planned_length':str(job.length_m or ''),'planned_composition':job.composition,'planned_wall_mm':str(job.wall_mm) if job.wall_mm is not None else None})
            for datafile in data.get('files',[]):
                key=store_file(datafile);stored.append(key)
                Attachment.objects.create(report=report,storage_key=key,**{k:datafile[k] for k in ['name','content_type','size','sha256']})
            if result or not_repaired:job.review_note=''
            if result:
                from projects.stock import reconcile_installation
                if couplings or old_ids:reconcile_installation(current,job,report,couplings)
                # Current installation can be corrected; old report snapshots remain immutable.
                CouplingUse.objects.filter(job=job).exclude(coupling__in=couplings).delete()
                for coupling in couplings:CouplingUse.objects.update_or_create(coupling=coupling,defaults={'job':job,'report':report})
                job.state='reported' if data['outcome']=='complete' else 'partial'
                if data['outcome']=='complete':job.problem='';job.problem_note=''
                if action=='correction':
                    audit(current,'repair_corrected',entity_id=str(job.pk),report_id=str(report.pk),previous_acceptance=str(job.accepted_date or ''),reason=report.note)
                    job.accepted_date=None;job.accepted_by=None;job.acceptance_basis=''
            elif action=='start':job.state='progress'
            elif action=='problem':
                job.problem=data['problem'];job.problem_note=report.note[:1000]
                if not_repaired:job.state='progress'
            elif action=='resolve':job.problem='';job.problem_note=''
            job.version+=1;job.save()
            audit(current,'repair_reported',entity_id=str(job.pk),report_id=str(report.pk),kind=kind)
            return report
    except Exception:
        for key in stored:private_path(key).unlink(missing_ok=True)
        raise

@transaction.atomic
def confirm_work(user,job_id,version):
    if not user.is_leader:raise ValidationError('Подтверждение доступно руководителю.')
    job=visible_jobs(user).select_for_update(no_key=True,of=('self',)).get(pk=job_id)
    if job.version!=version:raise ValidationError('Карточка изменилась. Проверьте актуальный результат.')
    report=job.reports.filter(kind__in=['complete','partial','correction']).first()
    if job.state!='reported' or not report or report.outcome!='complete':raise ValidationError('Нет завершённого результата для подтверждения.')
    report.confirmed_by=user;report.confirmed_at=timezone.now();report.save(update_fields=['confirmed_by','confirmed_at'])
    job.state='confirmed';job.version+=1;job.save(update_fields=['state','version'])
    audit(user,'repair_confirmed',entity_id=str(job.pk),report_id=str(report.pk))
    return job
