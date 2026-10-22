"""Versioned XLSX import: preview is rolled back; applying the snapshot is atomic."""
import uuid
from datetime import date
from django import forms
from django.core import signing
from django.core.exceptions import ValidationError,ObjectDoesNotExist
from django.db import transaction
from django.http import Http404,HttpResponse
from django.shortcuts import render,redirect
from django.contrib import messages
from accounts.permissions import permission_required
from accounts.views import audit
from documents.access import visible_documents
from repairs.excel import workbook
from repairs.importer import read_book
from .models import Equipment,EquipmentAssignment,EquipmentDocument,EquipmentImportReceipt,LegalEntity,COMPANY_TAX_ID
from .access import visible_objects,visible_contracts
from . import equipment as service

HEADERS={
 'register':['ID техники','Название / модель','Вид техники','Инвентарный номер','Госномер','Заводской номер / VIN','Принадлежность (own/rent/subcontractor)','Собственник','Состояние (ready/working/repair/reserve/archived)','Примечание','Версия'],
 'assignments':['ID назначения','ID техники','ID объекта','ID контракта','Начало работы','Окончание работы','Пояснение','Версия'],
 'documents':['ID связи','ID техники','ID документа','Вид документа','Действует до','Напоминать (0/1)','За сколько дней','Показывать (0/1)','Версия'],
}
class UploadForm(forms.Form):file=forms.FileField(label='Файл XLSX',widget=forms.FileInput(attrs={'accept':'.xlsx'}))
def check_kind(kind):
    if kind not in HEADERS:raise Http404

def export_rows(user,kind):
    if kind=='register':return [[str(i.pk),*[getattr(i,k) for k in service.FIELDS],i.version] for i in service.visible_equipment(user).order_by('inventory_number')]
    if kind=='assignments':
        allowed=set(visible_contracts(user).values_list('pk',flat=True))
        return [[str(a.pk),str(a.equipment_id),str(a.project_object_id),str(a.contract_id) if a.contract_id in allowed else '',str(a.start_date),str(a.end_date or ''),a.note,a.version] for a in service.placements(user).order_by('pk')]
    return [[str(r.pk),str(r.equipment_id),str(r.document_id),r.label,str(r.expires_on or ''),int(r.remind),r.days_before,int(r.active),r.version] for r in service.equipment_files(user).order_by('pk')]

@permission_required('equipment.manage')
def export(request,kind):
    check_kind(kind);data=workbook([] if request.GET.get('template')=='1' else export_rows(request.user,kind),headers=HEADERS[kind])
    result=HttpResponse(data,content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');result['Content-Disposition']=f'attachment; filename="armada-equipment-{kind}.xlsx"';result['Cache-Control']='private, no-store';return result

def day(value,required=False):
    if not value and not required:return None
    try:return date.fromisoformat(value)
    except ValueError:raise ValidationError('Дата должна быть в формате ГГГГ-ММ-ДД.')

def boolean(value):
    if value not in ('0','1'):raise ValidationError('Укажите 0 или 1.')
    return value=='1'

def apply_rows(user,kind,rows):
    actor=service.manager(user)
    LegalEntity.objects.select_for_update().get(tax_id=COMPANY_TAX_ID)
    try:ids={uuid.UUID(r['values'][0 if kind=='register' else 1]) for r in rows if r['values'][0 if kind=='register' else 1]}
    except ValueError:raise ValidationError('Проверьте ID техники.')
    list(Equipment.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
    for row in rows:
        v=row['values']
        try:
            if kind=='register':
                service.save_equipment(actor,uuid.UUID(v[0]) if v[0] else None,int(v[10]) if v[0] else None,dict(zip(service.FIELDS,v[1:10])))
            elif kind=='assignments':
                if v[0]:
                    a=service.placements(actor).get(pk=uuid.UUID(v[0]))
                    permitted_contracts=set(visible_contracts(actor).values_list('pk',flat=True))
                    contract=str(a.contract_id) if a.contract_id in permitted_contracts else ''
                    if [str(a.equipment_id),str(a.project_object_id),contract,str(a.start_date),a.note]!=[v[1],v[2],v[3],v[4],v[6]]:raise ValidationError('Место, технику, контракт и начало исторического назначения не переписывают. Создайте новое назначение с пустым ID.')
                    if a.version!=int(v[7]):raise ValidationError('Назначение изменено. Выгрузите актуальный файл.')
                    end=day(v[5])
                    if a.end_date!=end:
                        if a.end_date or not end:raise ValidationError('Завершённый период не переписывается.')
                        service.end_assignment(actor,a.pk,a.version,end)
                else:
                    item=service.visible_equipment(actor).get(pk=uuid.UUID(v[1]));obj=visible_objects(actor).get(pk=uuid.UUID(v[2]));contract=visible_contracts(actor).get(pk=uuid.UUID(v[3])) if v[3] else None
                    service.assign(actor,item.pk,item.version,obj,contract,day(v[4],True),day(v[5]),v[6])
            else:
                item=service.visible_equipment(actor).get(pk=uuid.UUID(v[1]));document=visible_documents(actor).get(pk=uuid.UUID(v[2]))
                service.save_file(actor,item.pk,uuid.UUID(v[0]) if v[0] else None,int(v[8]) if v[0] else None,{'document':document,'label':v[3],'expires_on':day(v[4]),'remind':boolean(v[5]),'days_before':int(v[6]),'active':boolean(v[7])})
        except (ValidationError,ValueError,TypeError,ObjectDoesNotExist) as e:
            detail='; '.join(e.messages) if isinstance(e,ValidationError) else 'Проверьте ID, версии и значения.'
            raise ValidationError(f'Строка {row["row"]}: {detail}') from e

def parse_rows(file,kind,user):
    book=read_book(file);cells=book['cells'];headers=HEADERS[kind]
    if book['formulas']:raise ValidationError('Формулы не поддерживаются. Вставьте значения.')
    if [str(cells.get((1,i+1),'')).strip() for i in range(len(headers))]!=headers:raise ValidationError('Столбцы не совпадают с шаблоном выбранного реестра.')
    rows=[];seen=set()
    for n in sorted({r for r,c in cells if r>1}):
        values=[str(cells.get((n,i+1),'')).strip() for i in range(len(headers))]
        if not any(values):continue
        if len(rows)>=500:raise ValidationError('За один импорт — до 500 строк.')
        if values[0] and values[0] in seen:raise ValidationError(f'Строка {n}: повтор ID.')
        if values[0]:seen.add(values[0])
        rows.append({'row':n,'values':values})
    if not rows:raise ValidationError('Нет строк для переноса.')
    with transaction.atomic():
        apply_rows(user,kind,rows);transaction.set_rollback(True)
    return rows

@permission_required('equipment.manage')
def upload(request,kind):
    check_kind(kind);form=UploadForm(request.POST or None,request.FILES or None);rows=None;preview='';status=200
    if request.method=='POST':
        if request.POST.get('apply')=='1':
            try:
                payload=signing.loads(request.POST.get('preview',''),salt='equipment-excel',max_age=3600)
                if payload['kind']!=kind or payload['actor']!=str(request.user.pk):raise ValidationError('Этот просмотр относится к другому пользователю или реестру.')
                with transaction.atomic():
                    actor=service.manager(request.user)
                    receipt=EquipmentImportReceipt.objects.filter(token=payload['token']).first()
                    if receipt and receipt.actor_id!=actor.pk:raise ValidationError('Чужой импорт.')
                    if not receipt:
                        apply_rows(actor,kind,payload['rows']);EquipmentImportReceipt.objects.create(token=payload['token'],actor=actor)
                        audit(actor,'equipment_imported',kind=kind,rows=len(payload['rows']))
            except (signing.BadSignature,ValidationError,ValueError,KeyError,TypeError) as e:form.add_error(None,'; '.join(e.messages) if isinstance(e,ValidationError) else 'Просмотр устарел или некорректен.');status=409
            else:messages.success(request,'Проверенные строки перенесены.');return redirect('equipment')
        elif form.is_valid():
            try:
                rows=parse_rows(form.cleaned_data['file'],kind,request.user)
                preview=signing.dumps({'actor':str(request.user.pk),'kind':kind,'rows':rows,'token':str(uuid.uuid4())},salt='equipment-excel',compress=True)
            except ValidationError as e:form.add_error('file',e)
    return render(request,'projects/equipment_excel.html',{'form':form,'rows':rows,'preview':preview,'headers':HEADERS[kind],'kind':kind},status=status)
