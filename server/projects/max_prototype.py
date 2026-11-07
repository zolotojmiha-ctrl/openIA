"""Offline MAX conversation prototype. No network, webhook or production session store.

The caller must supply a trusted MAX sender/private chat pair; unsigned HTTP
payloads must never call this module. This memory-only prototype is for synthetic
validation while the organisation/bot is being registered.
"""
import copy
import io
import warnings
from pathlib import Path
from PIL import Image, UnidentifiedImageError
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from accounts.models import User
from repairs.access import reportable_jobs
from repairs.forms import ReportForm
from repairs.models import Report
from repairs.services import report_work

@dataclass(frozen=True)
class Binding:
    user_id: object
    session_version: int
    max_user: int
    chat: int
    generation: object

@dataclass
class Draft:
    binding: Binding
    job_id: object
    action: str
    data: dict
    expires: object
    report_id: object = None

class Prototype:
    """Single-process test session; restart discards links/drafts. Not for deployment."""
    def __init__(self):
        self.codes = {}; self.pending = {}; self.bindings = {}; self.drafts = {}

    @staticmethod
    def employee(user):
        fresh = User.objects.get(pk=user.pk)
        if not fresh.is_active or fresh.role != 'foreman' or fresh.session_version != user.session_version:
            raise PermissionDenied('Доступ мастера изменился.')
        return fresh

    def issue_link(self, user):
        user = self.employee(user)
        code = secrets.token_urlsafe(32)
        self.codes[code] = (user.pk, user.session_version, timezone.now()+timedelta(minutes=10))
        return code

    def request_link(self, code, max_user, chat):
        # MAX IDs are numeric, not names/phone numbers. The code alone does not activate access.
        if type(max_user) is not int or type(chat) is not int or max_user <= 0 or chat <= 0:
            raise ValidationError('Нужен личный диалог MAX.')
        row = self.codes.pop(code, None)
        if not row or row[2] <= timezone.now():
            raise ValidationError('Ссылка привязки истекла или уже использована.')
        ident = uuid.uuid4()
        self.pending[ident] = (Binding(row[0], row[1], max_user, chat, uuid.uuid4()), row[2])
        return ident

    def approve_link(self, user, ident):
        # This step belongs in the authenticated personal cabinet, not a MAX message.
        user = self.employee(user)
        row = self.pending.get(ident)
        if not row or row[1] <= timezone.now() or row[0].user_id != user.pk or row[0].session_version != user.session_version:
            raise PermissionDenied('Привязку должен подтвердить её владелец в ЛК.')
        binding = row[0]
        key = (binding.max_user, binding.chat)
        if any(v.max_user == binding.max_user and v.user_id != user.pk for v in self.bindings.values()):
            raise ValidationError('Этот аккаунт MAX уже привязан к другому сотруднику.')
        self.revoke(user)
        self.bindings[key] = binding
        del self.pending[ident]

    def revoke(self, user):
        user = self.employee(user)
        self.bindings = {k:v for k,v in self.bindings.items() if v.user_id != user.pk}

    def identity(self, max_user, chat):
        binding = self.bindings.get((max_user, chat))
        if not binding:
            raise PermissionDenied('Сначала привяжите MAX через личный кабинет.')
        user = User.objects.get(pk=binding.user_id)
        if not user.is_active or user.role != 'foreman' or user.session_version != binding.session_version:
            raise PermissionDenied('Доступ изменился. Требуется новая привязка.')
        return binding, user

    def jobs(self, max_user, chat):
        _, user = self.identity(max_user, chat)
        return list(reportable_jobs(user).order_by('pk').values('pk','section__project_object__name','section__name','method','length_m')[:20])

    def prepare(self, max_user, chat, job_id, action, values, files=None):
        binding, user = self.identity(max_user, chat)
        if action not in ('result','problem'):
            raise ValidationError('Прототип принимает результат или сообщение о препятствии.')
        job = reportable_jobs(user).filter(pk=job_id).first()
        if not job:
            raise PermissionDenied('Нет доступа к этой работе.')
        if job.state in ('reported','confirmed','unrepairable'):
            raise ValidationError('По работе уже требуется другое действие. Откройте карточку в ЛК.')
        # Never trust caller-supplied idempotency tokens or optimistic-lock versions.
        data = dict(values, token=uuid.uuid4(), expected_version=job.version)
        form = ReportForm(data, files, job=job, action=action, user=user)
        if not form.is_valid():
            raise ValidationError(form.errors.as_json())
        # Standard attachments may intentionally remain opaque. A MAX photo,
        # however, must decode as an image before the confirmation is offered.
        for file in form.cleaned_data.get('files', []):
            if Path(file['name']).suffix.lower() in ('.png', '.jpg', '.jpeg'):
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter('error', Image.DecompressionBombWarning)
                        with Image.open(io.BytesIO(file['content'])) as photo:
                            if photo.format not in ('PNG', 'JPEG'):
                                raise ValueError('Unsupported photo format')
                            photo.verify()
                        with Image.open(io.BytesIO(file['content'])) as photo:
                            photo.load()
                except (UnidentifiedImageError, OSError, SyntaxError, ValueError,
                        Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                    raise ValidationError('Фотография повреждена или имеет недопустимый формат.') from exc
        token = data['token']
        self.drafts[token] = Draft(binding, job.pk, form.action, copy.deepcopy(form.cleaned_data), timezone.now()+timedelta(minutes=30))
        return {'confirmation_id':str(token), 'object':job.section.project_object.name,
                'section':job.section.name, 'defects':[d.number for d in job.active_defects],
                'action':action, 'method':form.cleaned_data.get('method'),
                'length_m':str(form.cleaned_data.get('length_m') or ''),
                'note':form.cleaned_data.get('note',''), 'files':len(form.cleaned_data.get('files',[]))}

    @transaction.atomic
    def confirm(self, max_user, chat, token, *, confirmed):
        binding, user = self.identity(max_user, chat)
        if confirmed is not True:
            raise ValidationError('Сначала подтвердите подготовленный отчёт.')
        draft = self.drafts.get(uuid.UUID(str(token)))
        if not draft or draft.binding != binding:
            raise PermissionDenied('Черновик принадлежит другому диалогу или не найден.')
        if not reportable_jobs(user).filter(pk=draft.job_id).exists():
            raise PermissionDenied('Доступ к работе отозван.')
        if draft.report_id:
            return Report.objects.get(pk=draft.report_id, author=user)
        if draft.expires <= timezone.now():
            raise ValidationError('Черновик истёк. Подготовьте отчёт заново.')
        report = report_work(user, draft.job_id, draft.action, draft.data)
        draft.report_id = report.pk
        return report
