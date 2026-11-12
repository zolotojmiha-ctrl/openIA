import uuid
from datetime import time,timedelta
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError,PermissionDenied
from django.http import HttpResponse
from django.shortcuts import render,redirect
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters,sensitive_variables
from django.contrib.auth.decorators import login_required
from .models import DiskBackupSettings,DiskBackupJob,PhotoArchive
from . import disk_backups
from .yandex_disk import DiskError

class SettingsForm(forms.Form):
    version=forms.IntegerField(widget=forms.HiddenInput)
    token=forms.CharField(label='OAuth-токен',required=False,max_length=8192,widget=forms.PasswordInput(render_value=False,attrs={'autocomplete':'new-password'}),help_text='Токен не отображается после сохранения. Оставьте пустым, чтобы сохранить прежний.')
    photos_enabled=forms.BooleanField(label='Копировать фотографии ремонтов на Яндекс Диск с проверкой целостности',required=False,help_text='Локальные оригиналы пока сохраняются. Фотографии остаются закрытыми.')
    photos_remove_local=forms.BooleanField(label='Освобождать место: удалять локальные фото старше 7 дней после проверки Диска и полной резервной копии',required=False,help_text='Оригиналы останутся на закрытом Диске и будут открываться через ЛК. При сбое проверки локальная копия сохраняется.')
    enabled=forms.BooleanField(label='Ежедневные резервные копии на Яндекс Диск',required=False)
    backup_time=forms.TimeField(label='Время по Омску',initial=time(3),widget=forms.TimeInput(format='%H:%M',attrs={'type':'time'}))

@never_cache
@login_required
@sensitive_post_parameters('token')
@sensitive_variables('private')
def settings(request):
    if not request.user.is_active or not request.user.is_leader:raise PermissionDenied
    config=DiskBackupSettings.objects.filter(pk=1).first()
    if config is None:config=DiskBackupSettings(pk=1)
    initial={'version':config.version,'enabled':config.enabled,'photos_enabled':config.photos_enabled,'photos_remove_local':config.photos_remove_local,'backup_time':config.backup_time}
    bound=request.POST if request.method=='POST' and request.POST.get('action')=='save' else ({**initial,'token':''} if request.method=='POST' else None)
    form=SettingsForm(bound,initial=initial)
    if request.method=='POST':
        try:
            action=request.POST.get('action')
            if action=='save':
                if form.is_valid():
                    disk_backups.save(request.user,form.cleaned_data['version'],form.cleaned_data['token'],form.cleaned_data['enabled'],form.cleaned_data['backup_time'],form.cleaned_data['photos_enabled'],form.cleaned_data['photos_remove_local'])
                    messages.success(request,'Настройки сохранены. Токен скрыт.');return redirect('disk_backup_settings')
            elif action=='check':
                disk_backups.check(request.user,int(request.POST['version']))
                messages.success(request,'Проверен доступ к информации о Диске и папке приложения. Запись проверяется при загрузке копии.');return redirect('disk_backup_settings')
            elif action=='key':
                private,fingerprint=disk_backups.create_key(request.user,int(request.POST['version']))
                response=HttpResponse(private,content_type='application/octet-stream')
                response['Content-Disposition']='attachment; filename="armada-recovery-'+fingerprint[:12]+'.pem"'
                response['Cache-Control']='private, no-store';response['Pragma']='no-cache';response['X-Content-Type-Options']='nosniff'
                return response
            elif action=='confirm-key':
                if request.POST.get('saved')!='yes':raise ValidationError('Подтвердите, что файл ключа сохранён отдельно от VPS.')
                disk_backups.confirm_key(request.user,request.POST.get('fingerprint',''))
                messages.success(request,'Сохранение ключа подтверждено.');return redirect('disk_backup_settings')
            elif action=='backup':
                disk_backups.enqueue(request.user,uuid.UUID(request.POST['token_id']))
                messages.success(request,'Задание создано. Обработчик на VPS создаст и проверит резервную копию.');return redirect('disk_backup_settings')
            else:raise ValidationError('Неизвестное действие.')
        except (ValidationError,DiskError) as exc:form.add_error(None,exc if isinstance(exc,ValidationError) else 'Не удалось выполнить операцию с Диском. Проверьте токен.')
        except DiskBackupSettings.DoesNotExist:form.add_error(None,'Сначала сохраните настройки подключения.')
        except (ValueError,KeyError):form.add_error(None,'Форма устарела. Обновите страницу.')
    jobs=DiskBackupJob.objects.all()[:30]
    alive=config.worker_seen_at and config.worker_seen_at>=timezone.now()-timedelta(minutes=15)
    return render(request,'projects/disk_backup_settings.html',{'form':form,'config':config,'photo_count':PhotoArchive.objects.count(),'photo_removed':PhotoArchive.objects.filter(local_removed_at__isnull=False).count(),'jobs':jobs,'has_token':bool(config.credential),'worker_alive':bool(alive),'job_token':uuid.uuid4()})
