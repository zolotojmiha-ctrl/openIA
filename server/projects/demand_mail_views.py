import uuid
from zoneinfo import ZoneInfo
from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404,render,redirect
from django.http import HttpResponse
from django.contrib import messages
from django.utils import timezone
from accounts.permissions import permission_required
from accounts.models import User
from postoffice.access import boxes
from postoffice.services import addresses
from repairs.access import visible_sections
from .access import visible_objects,visible_contracts
from .models import DemandMailing
from . import demand_mail
from .scope_widgets import ScopeChecklist

class MailingForm(forms.ModelForm):
    expected_version=forms.IntegerField(widget=forms.HiddenInput,initial=0)
    recipients=forms.CharField(label='Адреса получателей',widget=forms.Textarea(attrs={'rows':3}),help_text='До 20 адресов через запятую или с новой строки.')
    weekday=forms.TypedChoiceField(label='День недели',coerce=int,choices=list(enumerate(['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье'])))
    timezone=forms.ChoiceField(label='Часовой пояс',choices=[('Asia/Omsk','Омск'),('Asia/Novosibirsk','Новосибирск'),('Europe/Moscow','Москва')])
    class Meta:
        model=DemandMailing
        fields=['name','mailbox','recipients','project_objects','sections','contracts','enabled','weekday','send_time','timezone']
        labels={'mailbox':'Ящик отправителя','project_objects':'Объекты для этого заказчика','sections':'Участки (пусто — все выбранных объектов)','contracts':'Контракты (пусто — все доступные)','send_time':'Время отправки'}
        widgets={'project_objects':ScopeChecklist,'sections':ScopeChecklist,'contracts':ScopeChecklist,'send_time':forms.TimeInput(format='%H:%M',attrs={'type':'time'})}
    def __init__(self,*args,user,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['mailbox'].queryset=boxes(user).filter(state='active')
        self.fields['project_objects'].queryset=visible_objects(user).exclude(status='archived')
        self.fields['contracts'].queryset=visible_contracts(user).exclude(status='archived')
        self.fields['sections'].queryset=visible_sections(user).filter(project_object__in=visible_objects(user))
        self.initial['expected_version']=self.instance.version if self.instance.pk and not self.instance._state.adding else 0
    def clean_recipients(self):
        result=list(dict.fromkeys(addresses(self.cleaned_data['recipients'].replace('\r','').replace('\n',',').replace(';',','))))
        if not result:raise ValidationError('Укажите хотя бы один адрес.')
        return ', '.join(result)
    def clean(self):
        data=super().clean()
        if data.get('project_objects') and data.get('sections') and data['sections'].exclude(project_object__in=data['project_objects']).exists():self.add_error('sections','Выберите участки только выбранных объектов.')
        return data

def owned(user,pk):
    return get_object_or_404(DemandMailing,pk=pk,owner=user)

@permission_required('stock.manage')
def settings(request,pk=None):
    config=owned(request.user,pk) if pk else None
    form=MailingForm(request.POST or None,user=request.user,instance=config)
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                actor=User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
                demand_mail.manager(actor)
                if actor.session_version!=request.user.session_version:raise ValidationError('Доступ изменился. Войдите заново.')
                if config:
                    locked=DemandMailing.objects.select_for_update().get(pk=config.pk,owner=actor)
                    if locked.version!=form.cleaned_data['expected_version']:raise ValidationError('Настройки уже изменены. Откройте форму заново.')
                elif form.cleaned_data['expected_version']!=0:raise ValidationError('Настройки не найдены.')
                saved=form.save(commit=False);saved.owner=actor;saved.version=(locked.version+1 if config else 1);saved.not_before=timezone.now();saved.save();form.save_m2m()
        except ValidationError as error:form.add_error(None,error)
        else:
            messages.success(request,'Настройки сохранены. Автоматическая отправка работает только при включённом расписании.')
            return redirect('demand_mail_list')
    return render(request,'projects/demand_mail_form.html',{'form':form,'config':config})

@permission_required('stock.manage')
def listing(request):
    configs=DemandMailing.objects.filter(owner=request.user).select_related('mailbox').prefetch_related('project_objects','sections')
    return render(request,'projects/demand_mail_list.html',{'configs':configs})

@permission_required('stock.manage')
def preview(request,pk):
    config=owned(request.user,pk);data=None;error=None;token=None
    try:
        data=demand_mail.snapshot(config,request.user)
        token=signing.dumps({'mailing':str(config.pk),'actor':str(request.user.pk),'digest':data['digest'],'token':str(uuid.uuid4())},salt='demand-mail-preview')
        if request.method=='POST':
            shown=signing.loads(request.POST.get('preview',''),salt='demand-mail-preview',max_age=3600)
            if shown['actor']!=str(request.user.pk) or shown['mailing']!=str(config.pk):raise ValidationError('Предпросмотр относится к другой рассылке.')
            run=demand_mail.enqueue(request.user,config.pk,uuid.UUID(shown['token']),expected_digest=shown['digest'])
            messages.success(request,'Потребность поставлена в почтовую очередь. Это ещё не подтверждение получения заказчиком.')
            return redirect('demand_mail_history',pk=config.pk)
        if request.GET.get('export')=='xlsx':
            from .demand_mail_xlsx import workbook
            response=HttpResponse(workbook(data),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Content-Disposition']='attachment; filename="customer-demand.xlsx"';response['Cache-Control']='private, no-store';return response
    except (ValidationError,signing.BadSignature,ValueError,KeyError) as exc:
        error='; '.join(exc.messages) if isinstance(exc,ValidationError) else 'Предпросмотр устарел или повреждён. Обновите страницу.'
    return render(request,'projects/demand_mail_preview.html',{'config':config,'data':data,'preview':token,'error':error,'subject':demand_mail.SUBJECT},status=400 if error else 200)

@permission_required('stock.manage')
def history(request,pk):
    config=owned(request.user,pk)
    runs=config.runs.prefetch_related('deliveries__letter__attachments').order_by('-created_at')[:50]
    return render(request,'projects/demand_mail_history.html',{'config':config,'runs':runs})
