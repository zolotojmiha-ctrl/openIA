from .passwords import temporary_password
"""Connect an HR record to a login without implicit file or project access."""
from django import forms
from django.core.exceptions import PermissionDenied
from django.db import transaction,IntegrityError
from django.shortcuts import get_object_or_404,render,redirect
from django.contrib import messages
from django.views.decorators.debug import sensitive_variables,sensitive_post_parameters
from .models import User,AuditEvent,temporary_deadline,normalize_phone,ROLES
from .permissions import permission_required,can_link_person,manageable_user
from documents.access import visible_people
from documents.models import Person

class AccountForm(forms.Form):
    revision=forms.IntegerField(widget=forms.HiddenInput)
    mode=forms.ChoiceField(label='Действие',choices=[('create','Создать учётную запись'),('link','Связать существующую')])
    account=forms.ModelChoiceField(label='Существующая учётная запись',queryset=User.objects.none(),required=False)
    phone=forms.CharField(label='Телефон для новой записи',required=False,max_length=40)
    role=forms.ChoiceField(label='Рабочая роль',choices=ROLES,required=False)
    def __init__(self,*args,actor,**kwargs):
        super().__init__(*args,**kwargs)
        ids=[u.pk for u in User.objects.filter(leader_slot__isnull=True,person_card__isnull=True) if manageable_user(actor,u)]
        self.fields['account'].queryset=User.objects.filter(pk__in=ids).order_by('full_name')
        self.fields['account'].label_from_instance=lambda u:f'{u.full_name} · {u.phone}'+(' · Заблокирована' if not u.is_active else '')
    def clean(self):
        c=super().clean()
        if c.get('mode')=='link' and not c.get('account'):self.add_error('account','Выберите учётную запись.')
        if c.get('mode')=='create':
            if not c.get('phone'):self.add_error('phone','Укажите телефон.')
            else:
                try:c['phone']=normalize_phone(c['phone'])
                except forms.ValidationError as e:self.add_error('phone',e)
                else:
                    if User.objects.filter(phone=c['phone']).exists():self.add_error('phone','Телефон уже используется. Выберите существующую запись.')
            if not c.get('role'):self.add_error('role','Выберите рабочую роль.')
        return c

@permission_required('users.manage')
@sensitive_variables('temporary')
@sensitive_post_parameters()
def person_account(request,pk):
    if not can_link_person(request.user):raise PermissionDenied
    person=get_object_or_404(visible_people(request.user),pk=pk,archived=False)
    form=AccountForm(request.POST or None,actor=request.user,initial={'revision':person.revision,'role':'foreman'})
    if request.method=='POST' and form.is_valid():
        temporary=None
        try:
            with transaction.atomic():
                actor=User.objects.select_for_update().get(pk=request.user.pk)
                if not can_link_person(actor):raise PermissionDenied
                person=Person.objects.select_for_update().get(pk=person.pk)
                if person.archived:raise PermissionDenied
                if person.revision!=form.cleaned_data['revision'] or person.user_id:form.add_error(None,'Карточка уже связана или изменена. Обновите страницу.')
                else:
                    if form.cleaned_data['mode']=='create':
                        temporary=temporary_password()
                        account=User.objects.create_user(form.cleaned_data['phone'],temporary,full_name=person.full_name,role=form.cleaned_data['role'],temporary_expires_at=temporary_deadline())
                    else:
                        account=User.objects.select_for_update().get(pk=form.cleaned_data['account'].pk)
                        if not manageable_user(actor,account) or Person.objects.filter(user=account).exists():raise PermissionDenied
                    person.user=account;person.revision+=1;person.save(update_fields=['user','revision'])
                    AuditEvent.objects.create(actor=actor,subject=account,action='employee_created' if temporary else 'employee_updated',details={'kind':'person_account_link','person':str(person.pk),'new':bool(temporary)})
        except IntegrityError:form.add_error(None,'Учётная запись уже связана или телефон занят. Проверьте карточку.')
        else:
            if not form.errors:
                if temporary:return render(request,'accounts/temporary.html',{'employee':account,'temporary':temporary,'person':person})
                messages.success(request,'Карточка связана. Доступ к объектам, контрактам и файлам выдаётся отдельно.')
                return redirect('person_detail',pk=person.pk)
    return render(request,'accounts/person_account.html',{'form':form,'person':person})
