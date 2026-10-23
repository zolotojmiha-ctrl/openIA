from projects.archive import ArchiveProtectedModel
"""Engineering records. Plans, field reports and customer acceptance stay separate."""
import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from .values import kilometre

METHODS = [('p1','П1'),('p2','П2'),('p3','П3'),('p4','П4'),('p5u','П5У'),('grind','Шлифовка'),('cut','Вырезка'),('other','Другой способ')]
STATES = [('planned','Не устранён'),('progress','Не устранён — в работе'),('partial','Не устранён — частично выполнено'),('reported','Устранён — на проверке'),('confirmed','Устранён')]
PROBLEMS = [('','Нет открытой проблемы'),('materials','Нет материалов'),('access','Нет проезда / доступа'),('water','Котлован затоплен'),('other','Другая причина'),('swamp','Болото'),('coupling','Нет муфты'),('material_other','Нет другого материала'),('not_started','Ещё не выполняли'),('winter','Нет проезда — доступ зимой'),('existing_coupling','Обнаружена ранее установленная муфта'),('oversize_p1','Нет муфты П1 увеличенного диаметра')]

class Section(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    project_object=models.ForeignKey('projects.ProjectObject',on_delete=models.PROTECT,related_name='repair_sections')
    rnu=models.CharField('РНУ',max_length=120)
    pipeline=models.CharField('Трубопровод',max_length=200)
    name=models.CharField('Участок',max_length=200)
    diameter_mm=models.PositiveIntegerField('Наружный диаметр, мм',null=True,blank=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['project_object','rnu','pipeline','name'],name='repair_section_identity')]
    def __str__(self):return f'{self.rnu} · {self.pipeline} · {self.name}'

class Defect(ArchiveProtectedModel):
    contract=models.ForeignKey('projects.Contract',verbose_name='Контракт',on_delete=models.PROTECT,null=True,blank=True,related_name='repair_defects')
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    section=models.ForeignKey(Section,on_delete=models.PROTECT,related_name='defects')
    kilometre=models.DecimalField('Километр',max_digits=12,decimal_places=6)
    number=models.CharField('Номер дефекта',max_length=80)
    pipe_section=models.CharField('Номер секции трубы',max_length=80,blank=True)
    archived_at=models.DateTimeField(null=True,blank=True)
    archived_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name='+')
    description=models.TextField('Описание',max_length=3000,blank=True)
    latitude=models.DecimalField('Широта',max_digits=9,decimal_places=6,null=True,blank=True)
    longitude=models.DecimalField('Долгота',max_digits=9,decimal_places=6,null=True,blank=True)
    source=models.JSONField(default=dict,blank=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['section','kilometre','number'],condition=models.Q(contract__isnull=True),name='repair_legacy_defect_identity'),models.UniqueConstraint(fields=['contract','section','kilometre','number'],condition=models.Q(contract__isnull=False),name='repair_contract_defect_identity'),models.CheckConstraint(condition=models.Q(kilometre__gte=0),name='repair_km_positive')]
    def __str__(self):return f'км {self.km} · № {self.number}'
    @property
    def km(self):return format(kilometre(self.kilometre),'.3f')

class Job(ArchiveProtectedModel):
    subcontract_order=models.ForeignKey('projects.SubcontractOrder',on_delete=models.PROTECT,null=True,blank=True,related_name='repair_jobs')
    contract=models.ForeignKey('projects.Contract',verbose_name='Контракт',on_delete=models.PROTECT,null=True,blank=True,related_name='repair_jobs')
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    section=models.ForeignKey(Section,on_delete=models.PROTECT,related_name='jobs')
    defects=models.ManyToManyField(Defect,through='JobDefect',related_name='jobs')
    assigned_to=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name='repair_jobs')
    method=models.CharField('Метод ремонта',max_length=20,choices=METHODS)
    length_m=models.DecimalField('Длина конструкции, м',max_digits=9,decimal_places=3,null=True,blank=True)
    wall_mm=models.DecimalField('Толщина стенки муфты, мм',max_digits=6,decimal_places=2,null=True,blank=True)
    composition=models.CharField('Состав конструкции, м',max_length=200,blank=True)
    planned_date=models.DateField('Плановая дата',null=True,blank=True)
    notes=models.TextField('Задание и условия работ',max_length=3000,blank=True)
    materials=models.CharField('Материалы',max_length=20,choices=[('unknown','Не проверено'),('ready','Всё необходимое есть'),('missing','Есть нехватка')],default='unknown')
    state=models.CharField(max_length=20,choices=STATES,default='planned')
    review_note=models.CharField('Возврат на доработку',max_length=3000,blank=True)
    crew_assignment=models.ForeignKey('projects.BrigadeAssignment',on_delete=models.PROTECT,null=True,blank=True,related_name='jobs')
    problem=models.CharField(max_length=20,choices=PROBLEMS,blank=True)
    problem_note=models.CharField(max_length=1000,blank=True)
    version=models.PositiveIntegerField(default=1)
    accepted_date=models.DateField('Принято заказчиком',null=True,blank=True)
    accepted_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name='+')
    acceptance_basis=models.CharField('Основание приёмки',max_length=500,blank=True)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    created_at=models.DateTimeField(auto_now_add=True)
    source=models.JSONField(default=dict,blank=True)
    class Meta:
        constraints=[models.CheckConstraint(condition=models.Q(length_m__isnull=True)|models.Q(length_m__gt=0),name='repair_length_positive')]
    @property
    def defect_status(self):return 'Устранён' if self.state in ('reported','confirmed') else 'Не устранён'
    @property
    def status_tone(self):
        if self.state in ('reported','confirmed'):return 'resolved'
        if self.problem in ('materials','coupling','material_other','oversize_p1'):return 'materials'
        if self.problem in ('access','water','swamp','winter'):return 'blocked'
        return 'neutral'
    @property
    def verification_label(self):return {'reported':'На проверке руководителя','confirmed':'Проверено руководителем'}.get(self.state,'')
    @property
    def reason_display(self):
        if self.review_note:return 'Возвращён на доработку: '+self.review_note
        if self.problem:return self.get_problem_display()
        return {'planned':'Ещё не выполняли','progress':'Работы продолжаются','partial':'Выполнено частично'}.get(self.state,'')
    @property
    def wall_display(self):
        if self.method not in ('p1','p2','p3','p4','p5u') or self.wall_mm is None:return '—'
        return str(int(self.wall_mm)) if self.wall_mm==self.wall_mm.to_integral_value() else 'Требует уточнения'
    @property
    def active_defects(self):return [d for d in self.defects.all() if d.archived_at is None]
    @property
    def archived_defects(self):return [d for d in self.defects.all() if d.archived_at is not None]
    @property
    def repair_label(self):return self.source.get('repair_code') or str(self.pk)
    def __str__(self):return f'{self.section.name} · {self.get_method_display()}'

class JobDefect(ArchiveProtectedModel):
    job=models.ForeignKey(Job,on_delete=models.PROTECT)
    defect=models.ForeignKey(Defect,on_delete=models.PROTECT)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['job','defect'],name='repair_job_defect')]
    def clean(self):
        if self.job.contract_id!=self.defect.contract_id:raise ValidationError('Дефект и работа должны относиться к одному контракту.')
        if self.job.section_id!=self.defect.section_id:raise ValidationError('Дефект должен относиться к тому же участку.')
    def save(self,*args,**kwargs):
        self.clean();return super().save(*args,**kwargs)

class Coupling(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    project_object=models.ForeignKey('projects.ProjectObject',on_delete=models.PROTECT)
    method=models.CharField('Тип муфты',max_length=20,choices=METHODS[:5])
    serial=models.CharField('Заводской номер',max_length=100,blank=True)
    length_mm=models.PositiveIntegerField('Длина, мм')
    wall_mm=models.DecimalField('Толщина стенки муфты, мм',max_digits=6,decimal_places=2,null=True,blank=True)
    warehouse=models.CharField('Место хранения',max_length=200,blank=True)
    source=models.JSONField(default=dict,blank=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['project_object','method','serial'],condition=~models.Q(serial=''),name='repair_serial_identity'),models.CheckConstraint(condition=models.Q(length_mm__gt=0),name='repair_coupling_length_positive')]
    def __str__(self):return f'{self.get_method_display()} · {self.length_mm} мм · № {self.serial or "не указан"} · {self.warehouse or "место не указано"}'

class Report(ArchiveProtectedModel):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    job=models.ForeignKey(Job,on_delete=models.PROTECT,related_name='reports')
    author=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    token=models.UUIDField(default=uuid.uuid4,unique=True)
    request_digest=models.CharField(max_length=64,blank=True)
    kind=models.CharField('Действие',max_length=20,choices=[('start','Начали работу'),('problem','Сообщили о проблеме'),('resolve','Препятствие устранено'),('partial','Выполнено частично'),('complete','Работа выполнена'),('note','Фото / пояснение'),('correction','Исправление подтверждённого результата'),('acceptance','Приёмка заказчиком'),('imported','Сведения из ведомости'),('assignment','Смена ответственного мастера'),('revision','Возвращено на доработку')])
    performed_date=models.DateField('Дата')
    method=models.CharField('Фактический способ',max_length=20,choices=METHODS,blank=True)
    length_m=models.DecimalField('Фактическая длина, м',max_digits=9,decimal_places=3,null=True,blank=True)
    composition=models.CharField('Фактический состав, м',max_length=200,blank=True)
    note=models.TextField('Пояснение',max_length=3000,blank=True)
    problem=models.CharField(max_length=20,choices=PROBLEMS,blank=True)
    coupling_snapshot=models.JSONField(default=list,blank=True)
    context=models.JSONField(default=dict,blank=True)
    outcome=models.CharField(max_length=20,blank=True)
    latitude=models.DecimalField(max_digits=9,decimal_places=6,null=True,blank=True)
    longitude=models.DecimalField(max_digits=9,decimal_places=6,null=True,blank=True)
    confirmed_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name='+')
    confirmed_at=models.DateTimeField(null=True,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=['-created_at','-pk']

class CouplingUse(ArchiveProtectedModel):
    coupling=models.OneToOneField(Coupling,on_delete=models.PROTECT,related_name='installation')
    job=models.ForeignKey(Job,on_delete=models.PROTECT,related_name='installed_couplings')
    report=models.ForeignKey(Report,on_delete=models.PROTECT)

class Attachment(ArchiveProtectedModel):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    report=models.ForeignKey(Report,on_delete=models.PROTECT,related_name='attachments')
    storage_key=models.CharField(max_length=40)
    name=models.CharField(max_length=200)
    content_type=models.CharField(max_length=100)
    size=models.PositiveIntegerField()
    sha256=models.CharField(max_length=64)

class ImportBatch(ArchiveProtectedModel):
    contract=models.ForeignKey('projects.Contract',verbose_name='Контракт',on_delete=models.PROTECT,null=True,blank=True,related_name='repair_importbatchs')
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    section=models.ForeignKey(Section,on_delete=models.PROTECT)
    author=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    filename=models.CharField(max_length=200)
    storage_key=models.CharField(max_length=40,blank=True)
    size=models.PositiveIntegerField(default=0)
    sha256=models.CharField(max_length=64)
    sheet=models.CharField(max_length=150)
    payload=models.JSONField()
    applied_at=models.DateTimeField(null=True,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
