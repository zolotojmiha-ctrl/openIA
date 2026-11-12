"""User reports do not silently change equipment status or rental charges."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from accounts.models import User
from accounts.permissions import permitted
from accounts.views import audit
from .models import EquipmentIssue
from .equipment import placements,visible_equipment
from .access import visible_contracts,visible_objects
from .archive import writable


def visible(user):
    rows=EquipmentIssue.objects.filter(equipment__in=visible_equipment(user),placement__project_object__in=visible_objects(user),placement__contract__in=visible_contracts(user)).select_related('equipment','placement__contract','placement__project_object','author')
    return rows


@transaction.atomic
def submit(user,equipment_id,placement_id,description,token):
    actor=User.objects.select_for_update().get(pk=user.pk)
    if not actor.is_active or actor.session_version!=user.session_version:raise PermissionDenied
    placement=placements(actor).select_for_update(of=('self',)).filter(pk=placement_id,equipment_id=equipment_id,project_object__in=visible_objects(actor),contract__in=visible_contracts(actor)).first()
    if not placement:raise PermissionDenied
    writable([placement.contract_id])
    if placement.contract.status=='archived' or placement.project_object.status=='archived' or placement.equipment.status=='archived':raise ValidationError('Назначение или техника в архиве.')
    if placement.start_date>timezone.localdate() or placement.end_date and placement.end_date<timezone.localdate():raise ValidationError('Выберите действующее назначение техники.')
    description=description.strip()
    if not description or len(description)>3000:raise ValidationError('Опишите неисправность, до 3000 символов.')
    previous=EquipmentIssue.objects.filter(token=token).first()
    if previous:
        if previous.author_id!=actor.pk or previous.placement_id!=placement.pk or previous.description!=description:raise ValidationError('Отправка уже использована.')
        return previous
    issue=EquipmentIssue.objects.create(equipment_id=equipment_id,placement=placement,description=description,author=actor,token=token)
    audit(actor,'equipment_issue',issue=str(issue.pk))
    return issue


@transaction.atomic
def resolve(user,pk,note):
    actor=User.objects.select_for_update().get(pk=user.pk)
    if not actor.is_active or actor.session_version!=user.session_version or not permitted(actor,'equipment.manage'):raise PermissionDenied
    issue=visible(actor).select_for_update(of=('self',)).get(pk=pk)
    writable([issue.placement.contract_id])
    if issue.placement.contract.status=='archived' or issue.placement.project_object.status=='archived':raise ValidationError('Контракт или объект в архиве.')
    if not note.strip():raise ValidationError('Укажите результат рассмотрения.')
    if issue.state=='closed':return issue
    issue.state='closed';issue.resolution=note.strip()[:3000];issue.resolved_by=actor;issue.resolved_at=timezone.now();issue.save()
    audit(actor,'equipment_issue_closed',issue=str(issue.pk));return issue
