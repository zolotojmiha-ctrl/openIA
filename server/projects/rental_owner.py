"""Explicit external rental access. Never grants internal equipment or finance access."""
import uuid
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import render,redirect,get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
from accounts.models import User
from accounts.views import leader_required,audit
from .models import RentalAccess,RentalDispute,EquipmentRental
from . import rentals


def visible(user):
    rows=RentalAccess.objects.filter(active=True,rental__placement__equipment__legal_entity__tax_id=rentals.COMPANY_TAX_ID).select_related('rental__placement__equipment','rental__placement__project_object')
    return rows.filter(user=user) if user.is_authenticated and user.is_active and user.role=='lessor' and not user.is_leader else rows.none()


def grant(user,rental_id,owner_id,version,active,financial):
    with transaction.atomic():
        actor=rentals.actor_lock(user)
        if not actor.is_leader:raise PermissionDenied
        rental=rentals.visible(actor).select_for_update(of=('self',)).get(pk=rental_id)
        owner=User.objects.select_for_update().get(pk=owner_id)
        row=RentalAccess.objects.select_for_update().filter(rental=rental,user=owner).first()
        if owner.is_leader or (active and (owner.role!='lessor' or not owner.is_active)) or (not row and owner.role!='lessor'):raise ValidationError('Для открытия доступа выберите активную учётную запись арендодателя.')
        if row and row.version!=version or not row and version!=0:raise ValidationError('Доступ уже изменён. Обновите страницу.')
        if not row:row=RentalAccess(rental=rental,user=owner,granted_by=actor,version=0)
        row.active=active;row.financial=financial;row.version+=1;row.save()
        audit(actor,'equipment_assigned',kind='rental_external_access',rental=str(rental.pk),access=row.pk,active=active,financial=financial)
        return row


def dispute(user,access_id,day,text,token):
    with transaction.atomic():
        actor=rentals.actor_lock(user)
        access=visible(actor).select_for_update(of=('self',)).filter(pk=access_id).first()
        if not access:raise PermissionDenied
        rentals.period_dates(access.rental,day,day)
        if not str(text).strip() or len(text)>2000:raise ValidationError('Опишите разногласие, до 2000 символов.')
        previous=RentalDispute.objects.filter(token=token).first()
        if previous:
            if previous.access_id==access.pk and previous.date==day and previous.text==text.strip():return previous
            raise ValidationError('Эта отправка уже сохранена с другими данными.')
        row=RentalDispute.objects.create(access=access,date=day,text=text.strip(),token=token)
        audit(actor,'equipment_assigned',kind='rental_dispute',rental=str(access.rental_id),dispute=row.pk)
        return row


class DisputeForm(forms.Form):
    date=forms.DateField(label='Дата в табеле',widget=forms.DateInput(attrs={'type':'date'},format='%Y-%m-%d'))
    text=forms.CharField(label='Что нужно проверить',max_length=2000,widget=forms.Textarea(attrs={'rows':3}))
    token=forms.UUIDField(widget=forms.HiddenInput)


@login_required
def home(request):
    if request.user.role!='lessor':raise PermissionDenied
    rows=list(visible(request.user).order_by('pk'))
    for row in rows:row.hours=row.rental.days.aggregate(total=Sum('hours'))['total'] or 0
    return render(request,'projects/lessor_home.html',{'accesses':rows})


@login_required
def detail(request,pk):
    access=get_object_or_404(visible(request.user),rental_id=pk)
    form=DisputeForm(request.POST or None,initial={'date':min(access.rental.placement.end_date or timezone.localdate(),timezone.localdate()),'token':uuid.uuid4()})
    if request.method=='POST' and form.is_valid():
        try:dispute(request.user,access.pk,form.cleaned_data['date'],form.cleaned_data['text'],form.cleaned_data['token'])
        except ValidationError as error:form.add_error(None,error)
        else:
            messages.success(request,'Разногласие направлено на проверку. Табель не изменён.');return redirect('lessor_detail',pk=pk)
    days=access.rental.days.all()
    return render(request,'projects/lessor_detail.html',{'access':access,'rental':access.rental,'days':days,
        'hours':days.aggregate(total=Sum('hours'))['total'] or 0,'missing':rentals.missing_dates(access.rental,limit=5000),'form':form,'disputes':access.disputes.order_by('-created_at')[:100],
        'rates':access.rental.rates.all() if access.financial else [],'payments':access.rental.payments.filter(reversed=False) if access.financial else [],
        'totals':rentals.totals(access.rental) if access.financial else None})


@login_required
def export(request,pk):
    access=get_object_or_404(visible(request.user),rental_id=pk)
    from repairs.excel import workbook
    headers=['Дата','Отметка','Часы']+(['Начислено, ₽'] if access.financial else [])
    rows=[[str(d.date),d.get_kind_display(),d.hours]+([d.amount] if access.financial else []) for d in access.rental.days.all()]
    response=HttpResponse(workbook(rows,headers=headers,sheet_name='Табель аренды',numeric_columns=(2,3) if access.financial else (2,)),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition']='attachment; filename="rental-timesheet.xlsx"';response['Cache-Control']='private, no-store'
    return response


class GrantForm(forms.Form):
    user=forms.ModelChoiceField(label='Арендодатель',queryset=User.objects.none())
    expected_version=forms.IntegerField(widget=forms.HiddenInput,initial=0)
    active=forms.BooleanField(label='Доступ открыт',required=False,initial=True)
    financial=forms.BooleanField(label='Показывать тарифы, начисления и оплаты только этой аренды',required=False)
    def __init__(self,*args,rental=None,**kwargs):
        super().__init__(*args,**kwargs)
        from django.db.models import Q
        scope=Q(role='lessor')
        if rental is not None:scope|=Q(rental_accesses__rental=rental)
        self.fields['user'].queryset=User.objects.filter(scope,leader_slot__isnull=True).distinct().order_by('full_name')
        self.fields['user'].label_from_instance=lambda u:u.full_name


@leader_required
def manage(request,pk):
    rental=get_object_or_404(rentals.visible(request.user),pk=pk)
    row=None
    selected=request.GET.get('access')
    if selected:
        try:row=rental.external_accesses.get(pk=int(selected))
        except (ValueError,RentalAccess.DoesNotExist):raise PermissionDenied
    form=GrantForm(request.POST or None,rental=rental,initial={'user':row.user_id if row else None,'expected_version':row.version if row else 0,'active':row.active if row else True,'financial':row.financial if row else False})
    if request.method=='POST' and form.is_valid():
        try:grant(request.user,pk,form.cleaned_data['user'].pk,form.cleaned_data['expected_version'],form.cleaned_data['active'],form.cleaned_data['financial'])
        except ValidationError as error:form.add_error(None,error)
        else:
            messages.success(request,'Доступ арендодателя сохранён.');return redirect('rental_access',pk=pk)
    return render(request,'projects/rental_access.html',{'rental':rental,'form':form,'accesses':rental.external_accesses.select_related('user').all()})


@login_required
@require_POST
def resolve(request,pk):
    with transaction.atomic():
        actor=rentals.actor_lock(request.user,True)
        row=get_object_or_404(RentalDispute.objects.select_for_update().filter(access__rental__in=rentals.visible(actor)),pk=pk)
        note=request.POST.get('resolution','').strip()
        if not note or len(note)>2000:return HttpResponse('Укажите результат проверки до 2000 символов.',status=400)
        if not row.resolved_at:
            row.resolved_at=timezone.now();row.resolved_by=actor;row.resolution=note;row.save(update_fields=['resolved_at','resolved_by','resolution'])
            audit(actor,'equipment_assigned',kind='rental_dispute_resolved',rental=str(row.access.rental_id),dispute=row.pk)
    return redirect('rental_detail',pk=row.access.rental_id)
