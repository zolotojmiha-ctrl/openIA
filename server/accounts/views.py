import secrets
from datetime import timedelta
from functools import wraps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import FileResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_POST
from .forms import LoginForm, EmployeeForm, SecurePasswordChangeForm
from .models import User, AccessGrant, AuditEvent, LoginAttempt, PERMISSIONS, temporary_deadline


def leader_required(view):
    @login_required
    @wraps(view)
    def wrapper(request,*args,**kwargs):
        if not request.user.is_leader:
            raise PermissionDenied
        return view(request,*args,**kwargs)
    return wrapper

def audit(actor,action,subject=None,**details):
    AuditEvent.objects.create(actor=actor,subject=subject,action=action,details=details)

def consume_attempts(phone,ip):
    now=timezone.now()
    blocked=False
    with transaction.atomic():
        for kind,value,limit in [('phone',phone,8),('ip',ip,40)]:
            key=salted_hmac('armada.login.'+kind,value).hexdigest()
            entry,_=LoginAttempt.objects.select_for_update().get_or_create(key=key,defaults={'window_start':now})
            if now-entry.window_start>=timedelta(minutes=15):
                entry.count=0
                entry.window_start=now
            if entry.count>=limit:
                blocked=True
            entry.count+=1
            entry.save()
    return blocked

@sensitive_post_parameters('password')
def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    form=LoginForm(request.POST or None)
    if request.method=='POST':
        # Invalid phone syntax also consumes an IP attempt.
        valid=form.is_valid()
        phone=form.cleaned_data.get('phone','invalid') if valid else 'invalid'
        blocked=consume_attempts(phone,request.META.get('REMOTE_ADDR','unknown'))
        if blocked:
            form.add_error(None,'Слишком много попыток. Попробуйте через 15 минут.')
        elif valid:
            user=authenticate(request,username=phone,password=form.cleaned_data['password'])
            if user and not (user.must_change_password and (not user.temporary_expires_at or user.temporary_expires_at <= timezone.now())):
                login(request,user)
                LoginAttempt.objects.filter(key=salted_hmac('armada.login.phone',phone).hexdigest()).delete()
                audit(user,'login',user)
                return redirect('password' if user.must_change_password else 'dashboard')
            form.add_error(None,'Не удалось войти. Проверьте телефон и пароль.')
    return render(request,'accounts/login.html',{'form':form})

@login_required
@require_POST
def logout_view(request):
    audit(request.user,'logout',request.user)
    logout(request)
    return redirect('login')

@login_required
@sensitive_post_parameters('old_password','new_password1','new_password2')
def password_view(request):
    form=SecurePasswordChangeForm(request.user,request.POST or None)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            user=User.objects.select_for_update().get(pk=request.user.pk)
            if not user.check_password(form.cleaned_data['old_password']):
                form.add_error(None,'Пароль уже изменён. Войдите заново.')
                return render(request,'accounts/password.html',{'form':form})
            user.set_password(form.cleaned_data['new_password1'])
            user.must_change_password=False
            user.temporary_expires_at=None
            user.session_version+=1
            user.save()
            audit(user,'password_changed',user)
        update_session_auth_hash(request,user)
        messages.success(request,'Пароль изменён. Другие сеансы завершены.')
        return redirect('dashboard')
    return render(request,'accounts/password.html',{'form':form})

@login_required
def dashboard(request):
    from projects.access import visible_objects, visible_contracts,contract_card
    from documents.access import visible_documents,unread_documents
    from .personal import favorite_items
    from .models import Reminder, SupportRequest
    context={'object_count':visible_objects(request.user).count(),'contract_count':visible_contracts(request.user).count(),'document_count':visible_documents(request.user).filter(archived=False).count(),
        'recent_objects':visible_objects(request.user).order_by('name')[:5],'recent_contracts':[contract_card(c,request.user) for c in visible_contracts(request.user).order_by('id')[:5]],'new_documents':unread_documents(request.user).select_related('current_version').order_by('-current_version__created_at')[:5],
        'favorites':favorite_items(request.user),'reminders':Reminder.objects.filter(user=request.user,done=False).filter(document__isnull=True).order_by('date')[:5],
        'support_count':SupportRequest.objects.filter(status='new').count() if request.user.is_leader else 0}
    if request.user.is_leader:
        from finance.views import dashboard_data
        context.update(dashboard_data(request.user))
    return render(request,'accounts/dashboard.html',context)

@leader_required
def users(request):
    return render(request,'accounts/users.html',{'employees':User.objects.filter(leader_slot__isnull=True).order_by('full_name'),'leaders':User.objects.filter(leader_slot__isnull=False).order_by('leader_slot')})

@leader_required
@sensitive_post_parameters()
@sensitive_variables('temporary')
def employee_edit(request,pk=None):
    employee=get_object_or_404(User,pk=pk,leader_slot__isnull=True) if pk else None
    initial={'expected_version':employee.session_version,'full_name':employee.full_name,'phone':employee.phone,'role':employee.role,'is_active':employee.is_active,'permissions':[p for p in employee.grants.values_list('permission',flat=True) if p in PERMISSIONS]} if employee else None
    form=EmployeeForm(request.POST or None,initial=initial,user=employee)
    if request.method=='POST' and form.is_valid():
        temporary=None
        try:
            with transaction.atomic():
                if employee:
                    employee=User.objects.select_for_update().get(pk=employee.pk,leader_slot__isnull=True)
                    if form.cleaned_data['expected_version'] != employee.session_version:
                        form.add_error(None,'Настройки уже изменил другой руководитель. Обновите страницу и проверьте изменения.')
                        return render(request,'accounts/employee.html',{'form':form,'employee':employee})
                    before=list(employee.grants.values_list('permission',flat=True))
                else:
                    employee=User()
                    before=[]
                    temporary=secrets.token_urlsafe(20)
                    employee.set_password(temporary)
                employee.full_name=form.cleaned_data['full_name']
                employee.phone=form.cleaned_data['phone']
                employee.role=form.cleaned_data['role']
                employee.is_active=form.cleaned_data['is_active']
                employee.session_version+=1
                employee.save()
                if not employee.is_active:
                    from documents.models import Person
                    Person.objects.filter(user=employee).update(archived=True)
                employee.grants.all().delete()
                AccessGrant.objects.bulk_create([AccessGrant(user=employee,permission=p,granted_by=request.user) for p in form.cleaned_data['permissions']])
                audit(request.user,'employee_created' if temporary else 'employee_updated',employee,previous_permissions=before,permissions=form.cleaned_data['permissions'],active=employee.is_active)
        except IntegrityError:
            form.add_error('phone','Телефон уже используется. Обновите страницу и повторите.')
        else:
            if temporary:
                return render(request,'accounts/temporary.html',{'employee':employee,'temporary':temporary})
            messages.success(request,'Данные и права сохранены. Прежние сеансы сотрудника завершены.')
            return redirect('users')
    return render(request,'accounts/employee.html',{'form':form,'employee':employee})

@leader_required
@require_POST
@sensitive_variables('temporary')
def reset_password(request,pk):
    with transaction.atomic():
        user=get_object_or_404(User.objects.select_for_update(),pk=pk,leader_slot__isnull=True)
        temporary=secrets.token_urlsafe(20)
        user.set_password(temporary)
        user.must_change_password=True
        user.temporary_expires_at=temporary_deadline()
        user.session_version+=1
        user.save()
        audit(request.user,'employee_password_reset',user)
    return render(request,'accounts/temporary.html',{'employee':user,'temporary':temporary})

@leader_required
def audit_view(request):
    return render(request,'accounts/audit.html',{'events':AuditEvent.objects.select_related('actor','subject').order_by('-created_at')[:200]})

def public_site(request):
    response=FileResponse((settings.BASE_DIR/'index.html').open('rb'),content_type='text/html; charset=utf-8')
    return response

def prototype(request,filename='index.html'):
    if filename not in ['index.html','portal.css','portal.js']:
        raise Http404
    mime={'index.html':'text/html','portal.css':'text/css','portal.js':'text/javascript'}[filename]
    return FileResponse((settings.BASE_DIR/'portal'/filename).open('rb'),content_type=mime+'; charset=utf-8')
