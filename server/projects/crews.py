"""Dated crew rosters; assignment snapshots never follow later HR edits."""
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from accounts.models import User
from accounts.permissions import permitted
from accounts.views import audit
from documents.access import visible_people
from documents.models import Person,PersonAssignment
from .models import Brigade,BrigadeMember,BrigadeAssignment,Contract,ProjectObject,COMPANY_TAX_ID
from .access import visible_contracts,visible_objects


def visible_crews(user):
    qs=Brigade.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID)
    if not permitted(user,'personnel.manage'):
        qs=qs.filter(assignments__contract__in=visible_contracts(user),assignments__project_object__in=visible_objects(user)).distinct()
    return qs.select_related('foreman')

def visible_assignments(user):
    return BrigadeAssignment.objects.filter(contract__in=visible_contracts(user),project_object__in=visible_objects(user)).select_related('brigade','master','project_object')

def date_range(start,end):
    if not start or end and end<start:raise ValidationError('Проверьте начало и окончание периода.')

def roster_on(crew,date):
    return crew.memberships.filter(start_date__lte=date).filter(Q(end_date__isnull=True)|Q(end_date__gte=date)).select_related('person')

def manager(user):
    actor=User.objects.select_for_update().get(pk=user.pk)
    if not permitted(actor,'personnel.manage') or actor.session_version!=user.session_version:raise PermissionDenied
    return actor

def add_member_locked(actor,crew,person,start,end=None):
    date_range(start,end)
    if person.archived or person.legal_entity_id!=crew.legal_entity_id or not visible_people(actor).filter(pk=person.pk).exists():raise ValidationError('Сотрудник недоступен или находится в архиве.')
    overlap=crew.memberships.filter(person=person).filter(Q(end_date__isnull=True)|Q(end_date__gte=start))
    if end:overlap=overlap.filter(start_date__lte=end)
    if overlap.exists():raise ValidationError('У сотрудника уже есть участие в этой бригаде за выбранный период.')
    item=BrigadeMember.objects.create(brigade=crew,person=person,start_date=start,end_date=end,created_by=actor)
    crew.version+=1;crew.save(update_fields=['version'])
    audit(actor,'person_assigned',kind='brigade_member',brigade=str(crew.pk),person=str(person.pk),membership=item.pk,start=str(start),end=str(end or ''))
    return item

@transaction.atomic
def create_crew(user,name,foreman,members,start):
    actor=manager(user)
    if not name.strip() or len(name.strip())>160:raise ValidationError('Укажите название бригады до 160 символов.')
    crew=Brigade.objects.create(legal_entity=foreman.legal_entity,name=name.strip(),foreman=foreman,created_by=actor)
    people={p.pk:p for p in [foreman,*members]}
    for p in people.values():add_member_locked(actor,crew,p,start)
    audit(actor,'person_updated',kind='brigade_created',brigade=str(crew.pk),name=crew.name,foreman=str(foreman.pk))
    return crew

@transaction.atomic
def add_member(user,crew_id,version,person,start,end):
    actor=manager(user);crew=visible_crews(actor).select_for_update(of=('self',)).get(pk=crew_id)
    if crew.version!=version or crew.archived:raise ValidationError('Бригада изменена или в архиве. Обновите страницу.')
    return add_member_locked(actor,crew,person,start,end)

@transaction.atomic
def close_member(user,member_id,version,end):
    actor=manager(user);member=BrigadeMember.objects.select_related('brigade').get(pk=member_id)
    crew=visible_crews(actor).select_for_update(of=('self',)).get(pk=member.brigade_id)
    member=BrigadeMember.objects.select_for_update().get(pk=member_id)
    if member.version!=version or member.end_date:raise ValidationError('Участие уже изменено или завершено.')
    date_range(member.start_date,end)
    member.end_date=end;member.version+=1;member.save(update_fields=['end_date','version'])
    crew.version+=1;crew.save(update_fields=['version'])
    audit(actor,'person_assigned',kind='brigade_member_closed',membership=member.pk,brigade=str(crew.pk),end=str(end))
    return member

@transaction.atomic
def assign_crew_place(user,crew_id,version,contract_id,object_id,start,end):
    actor=manager(user)
    contract=Contract.objects.select_for_update().get(pk=contract_id)
    if not visible_contracts(actor).filter(pk=contract.pk).exists() or contract.status=='archived':raise ValidationError('Контракт недоступен или в архиве.')
    obj=ProjectObject.objects.get(pk=object_id)
    if not visible_objects(actor).filter(pk=obj.pk,contracts=contract).exists() or obj.status=='archived':raise ValidationError('Место не относится к доступным объектам контракта.')
    crew=visible_crews(actor).select_for_update(of=('self',)).get(pk=crew_id)
    if crew.version!=version or crew.archived:raise ValidationError('Состав бригады изменился или она в архиве. Обновите выбор.')
    date_range(start,end)
    members=list(roster_on(crew,start))
    if not members or not any(m.person_id==crew.foreman_id for m in members):raise ValidationError('Мастер должен входить в состав бригады на дату назначения.')
    if any(m.person.archived for m in members):raise ValidationError('В составе есть сотрудник из архива. Уточните состав.')
    master=crew.foreman.user
    if not master or not master.is_active:raise ValidationError('Свяжите мастера с активной учётной записью.')
    from repairs.views import assignees
    # Same grant checks as a manual foreman assignment. No access is granted here.
    if not master.is_leader and (not master.object_assignments.filter(project_object=obj).exists() or not master.contract_access.filter(contract=contract).exists()):raise ValidationError('Сначала выдайте мастеру доступ к объекту и контракту.')
    if BrigadeAssignment.objects.filter(brigade=crew,contract=contract,project_object=obj,start_date=start,end_date=end).exists():raise ValidationError('Такое назначение уже есть. Откройте существующую запись.')
    snapshot=[{'person':str(m.person_id),'name':m.person.full_name,'position':m.person.position,'membership':m.pk,'start':str(m.start_date),'end':str(m.end_date or '')} for m in members]
    assignment=BrigadeAssignment.objects.create(brigade=crew,contract=contract,project_object=obj,master=master,start_date=start,end_date=end,roster=snapshot,created_by=actor)
    for member in members:
        member_end=min(d for d in [end,member.end_date] if d) if end or member.end_date else None
        PersonAssignment.objects.create(person=member.person,project_object=obj,start_date=start,end_date=member_end,brigade_assignment=assignment,created_by=actor)
    audit(actor,'person_assigned',kind='brigade_place',brigade=str(crew.pk),assignment=str(assignment.pk),contract=str(contract.pk),object=str(obj.pk),roster=snapshot)
    return assignment

@transaction.atomic
def close_assignment(user,pk,version,end):
    actor=manager(user);assignment=visible_assignments(actor).select_for_update(of=('self',)).get(pk=pk)
    if assignment.version!=version or assignment.end_date:raise ValidationError('Назначение уже изменено или завершено.')
    date_range(assignment.start_date,end)
    assignment.end_date=end;assignment.version+=1;assignment.save(update_fields=['end_date','version'])
    for p in assignment.person_assignments.all():
        if p.end_date is None or p.end_date>end:p.end_date=end;p.save(update_fields=['end_date'])
    audit(actor,'person_assigned',kind='brigade_place_closed',assignment=str(pk),end=str(end))
    return assignment

@transaction.atomic
def assign_jobs_to_crew(user,assignment_id,versions):
    from repairs.access import can_prepare,visible_jobs
    from repairs.models import Job,Report
    actor=User.objects.select_for_update().get(pk=user.pk)
    if not can_prepare(actor) or actor.session_version!=user.session_version:raise PermissionDenied
    assignment=visible_assignments(actor).select_for_update(of=('self',)).get(pk=assignment_id)
    if assignment.end_date and assignment.end_date<timezone.localdate():raise ValidationError('Назначение бригады уже завершено.')
    if not assignment.master.is_active or (not assignment.master.is_leader and (not assignment.master.object_assignments.filter(project_object=assignment.project_object).exists() or not assignment.master.contract_access.filter(contract=assignment.contract).exists())):raise ValidationError('Доступ мастера изменился. Проверьте учётную запись и разрешения.')
    jobs=list(visible_jobs(actor).select_for_update(of=('self',)).filter(pk__in=versions).order_by('pk'))
    if not jobs or len(jobs)!=len(versions):raise ValidationError('Выбор работ изменился.')
    for j in jobs:
        if j.contract_id!=assignment.contract_id or j.section.project_object_id!=assignment.project_object_id or j.state=='confirmed' or j.version!=versions[str(j.pk)] or j.archived_defects:raise ValidationError('Одна из работ изменилась, завершена или относится к другому месту. Назначения не сохранены.')
    for j in jobs:
        if j.crew_assignment_id==assignment.pk and j.assigned_to_id==assignment.master_id:continue
        Report.objects.create(job=j,author=actor,kind='assignment',performed_date=timezone.localdate(),note='Назначена бригада: '+assignment.brigade.name+'. Прежние результаты и авторы сохранены.',context={'from_master':str(j.assigned_to_id or ''),'from_crew':str(j.crew_assignment_id or ''),'to_master':str(assignment.master_id),'to_crew':str(assignment.pk),'roster':assignment.roster})
        j.crew_assignment=assignment;j.assigned_to=assignment.master;j.version+=1;j.save(update_fields=['crew_assignment','assigned_to','version'])
        audit(actor,'repair_prepared',kind='crew_assignment',entity_id=str(j.pk),assignment=str(assignment.pk))
    return len(jobs)

@transaction.atomic
def update_crew(user,pk,version,name,foreman,archived=False):
    actor=manager(user);crew=visible_crews(actor).select_for_update(of=('self',)).get(pk=pk)
    if crew.version!=version:raise ValidationError('Бригада уже изменена. Обновите страницу.')
    if not name.strip() or len(name.strip())>160:raise ValidationError('Проверьте название бригады.')
    if foreman.archived or foreman.legal_entity_id!=crew.legal_entity_id or not visible_people(actor).filter(pk=foreman.pk).exists():raise ValidationError('Мастер недоступен.')
    if foreman.pk!=crew.foreman_id and not roster_on(crew,timezone.localdate()).filter(person=foreman).exists():raise ValidationError('Сначала добавьте нового мастера в действующий состав бригады.')
    if archived and crew.assignments.filter(Q(end_date__isnull=True)|Q(end_date__gte=timezone.localdate())).exists():raise ValidationError('Сначала завершите назначения бригады на объектах.')
    before={'name':crew.name,'foreman':str(crew.foreman_id),'archived':crew.archived}
    crew.name=name.strip();crew.foreman=foreman;crew.archived=archived;crew.version+=1;crew.save(update_fields=['name','foreman','archived','version'])
    audit(actor,'person_updated',kind='brigade_updated',brigade=str(crew.pk),previous=before,name=crew.name,foreman=str(foreman.pk),archived=archived)
    return crew

@transaction.atomic
def update_membership(user,pk,version,start,end):
    actor=manager(user);item=BrigadeMember.objects.get(pk=pk)
    crew=visible_crews(actor).select_for_update(of=('self',)).get(pk=item.brigade_id)
    item=BrigadeMember.objects.select_for_update().get(pk=pk)
    if item.version!=version:raise ValidationError('Период участия уже изменён.')
    date_range(start,end)
    overlap=crew.memberships.filter(person=item.person).exclude(pk=item.pk).filter(Q(end_date__isnull=True)|Q(end_date__gte=start))
    if end:overlap=overlap.filter(start_date__lte=end)
    if overlap.exists():raise ValidationError('Период участия пересекается с другой записью этого сотрудника.')
    before={'start':str(item.start_date),'end':str(item.end_date or '')}
    item.start_date=start;item.end_date=end;item.version+=1;item.save(update_fields=['start_date','end_date','version'])
    crew.version+=1;crew.save(update_fields=['version'])
    audit(actor,'person_assigned',kind='membership_dates_corrected',membership=pk,previous=before,start=str(start),end=str(end or ''))
    return item
