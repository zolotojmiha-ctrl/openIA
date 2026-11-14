"""Shared version selection, exact section-start distances and ACL-scoped neighbours."""
from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction
from django.utils import timezone
from accounts.models import User
from accounts.views import audit
from documents.storage import validate_upload,store_file,private_path
from .access import can_prepare,visible_sections,visible_jobs
from .models import PipeLayout,Section,Defect
from .layout_reader import parse,number


def manage_allowed(user):return can_prepare(user) and (user.is_leader or user.role not in ('customer','subcontractor','lessor'))

def actor(user,lock=False):
    row=(User.objects.select_for_update(no_key=True) if lock else User.objects).get(pk=user.pk)
    if not manage_allowed(row) or row.session_version!=user.session_version:raise PermissionDenied
    return row

def scoped(user,pk):
    return PipeLayout.objects.select_related('section__project_object').filter(pk=pk,section__in=visible_sections(user)).first()

def prepare(user,section_id,upload,stored_keys=None):
    actor(user)
    if not visible_sections(user).filter(pk=section_id).exists():raise PermissionDenied
    data=parse(upload);saved=None
    try:
        with transaction.atomic():
            fresh=actor(user,True);section=visible_sections(fresh).select_for_update(no_key=True,of=('self',)).get(pk=section_id)
            active=PipeLayout.objects.filter(section=section,state='active').first()
            if active and active.sha256==data['sha256']:return active
            content=validate_upload(upload);saved=store_file(content)
            if stored_keys is not None:stored_keys.append(saved)
            return PipeLayout.objects.create(section=section,author=fresh,filename=content['name'],storage_key=saved,
                sha256=data['sha256'],sheet=data['sheet'],data={'rows':data['rows']},session_version=fresh.session_version,basis_id=active.pk if active else None)
    except Exception:
        if saved:private_path(saved).unlink(missing_ok=True)
        raise

@transaction.atomic
def apply(user,pk):
    fresh=actor(user,True);candidate=scoped(fresh,pk)
    if not candidate:raise PermissionDenied
    Section.objects.select_for_update(no_key=True).get(pk=candidate.section_id)
    layout=PipeLayout.objects.select_for_update().get(pk=pk)
    if layout.state=='active':return layout
    if layout.author_id!=fresh.pk or layout.session_version!=fresh.session_version:raise PermissionDenied
    if layout.state!='preview' or layout.created_at<timezone.now()-timedelta(hours=24):raise ValidationError('Предпросмотр отменён или устарел. Загрузите раскладку заново.')
    active=PipeLayout.objects.filter(section_id=layout.section_id,state='active').first()
    if active and active.sha256==layout.sha256:return active
    if (active.pk if active else None)!=layout.basis_id:raise ValidationError('Другой пользователь уже изменил раскладку. Повторите предпросмотр.')
    if active:active.state='superseded';active.save(update_fields=['state'])
    layout.state='active';layout.applied_at=timezone.now();layout.save(update_fields=['state','applied_at'])
    audit(fresh,'pipe_layout_selected',entity_id=str(layout.section_id),layout=str(layout.pk),rows=len(layout.data['rows']))
    return layout

@transaction.atomic
def cancel(user,pk):
    fresh=actor(user,True);candidate=scoped(fresh,pk)
    if not candidate or candidate.author_id!=fresh.pk:raise PermissionDenied
    Section.objects.select_for_update(no_key=True).get(pk=candidate.section_id)
    layout=PipeLayout.objects.select_for_update().get(pk=pk)
    if layout.state=='preview':layout.state='cancelled';layout.save(update_fields=['state'])
    return layout.state

class Geometry:
    def __init__(self,layout):
        self.layout=layout;self.rows=layout.data['rows'];self.index={};self.prefix=[Decimal(0)];self.breaks=[0]
        for i,row in enumerate(self.rows):self.index.setdefault(row[0],[]).append(i)
        for i,row in enumerate(self.rows):
            length=Decimal(row[2]) if row[2] is not None else None
            self.prefix.append(self.prefix[-1]+(length or 0))
            bad=length is None or length<=0 or row[1] is None or len(self.index[row[0]])!=1
            if i+1<len(self.rows):
                nxt=self.rows[i+1]
                bad=bad or row[3]!=nxt[0] or not row[4] or row[4]!=nxt[4] or nxt[1] is None
                if not bad:bad=abs(Decimal(nxt[1])-Decimal(row[1])-length)>Decimal('.002')
            self.breaks.append(self.breaks[-1]+int(bad))
    def locate(self,defect):
        key=number(defect.pipe_section)
        if not key:return None,'Не указан номер секции.'
        indices=self.index.get(key,[])
        if not indices:return None,'Секция отсутствует в выбранной раскладке.'
        if len(indices)!=1:return None,'Номер секции повторяется в раскладке; нужна проверка.'
        i=indices[0];row=self.rows[i]
        if row[1] is None or row[2] is None or Decimal(row[2])<=0:return None,'Неизвестны начало или длина секции.'
        if row[6] is not None:
            trace=Decimal(row[6]);length=Decimal(row[2]);km=defect.kilometre*1000
            if km<trace-2 or km>trace+length+2:return None,'Секция не соответствует километру в раскладке; нужна проверка.'
        return i,''
    def distance(self,left,right):
        a,error=self.locate(left)
        if error:return {'reason':error}
        b,error=self.locate(right)
        if error:return {'reason':error}
        lo,hi=sorted((a,b))
        if self.breaks[hi]!=self.breaks[lo]:return {'reason':'Между секциями разрыв цепочки, смена прогона или несогласованные длины. Расстояние не рассчитано.'}
        value=(self.prefix[hi]-self.prefix[lo]).quantize(Decimal('.001'))
        return {'metres':value,'text':format(value,'.3f').replace('.',','),'trailer':value>6000,'same_section':a==b}

def status(layout):
    geo=Geometry(layout)
    return {'rows':len(geo.rows),'breaks':geo.breaks[-1],'duplicates':sum(len(v)>1 for v in geo.index.values())}

def neighbours(user,defects):
    """Neighbours in the whole visible contract/section, independent of page/search."""
    defects=list(defects);result={};contexts={}
    for d in defects:contexts[(d.contract_id,d.section_id)]=None
    for contract_id,section_id in contexts:
        jobs=visible_jobs(user).filter(contract_id=contract_id,section_id=section_id)
        sequence=list(Defect.objects.filter(jobs__in=jobs,archived_at__isnull=True).distinct().order_by('kilometre','number','pk'))
        positions={d.pk:i for i,d in enumerate(sequence)}
        layout=PipeLayout.objects.filter(section_id=section_id,state='active').first();geo=Geometry(layout) if layout else None
        for d in (x for x in defects if x.contract_id==contract_id and x.section_id==section_id):
            i=positions.get(d.pk)
            if i is None:continue
            pairs=[]
            for direction,j in [('previous',i-1),('next',i+1)]:
                if not 0<=j<len(sequence):continue
                other=sequence[j];left,right=(other,d) if direction=='previous' else (d,other)
                value=geo.distance(left,right) if geo else {'reason':'Для участка ещё не выбрана раскладка.'}
                pairs.append(dict(value,direction=direction,left=left,right=right,layout=layout))
            result[d.pk]=pairs
    return result
