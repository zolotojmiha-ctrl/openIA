from accounts import xlsx as human_xlsx
import uuid
from datetime import date
from decimal import Decimal,InvalidOperation
from django import forms
from django.core import signing
from django.core.exceptions import ValidationError,ObjectDoesNotExist
from django.db import transaction
from django.http import Http404,HttpResponse
from django.shortcuts import render,redirect
from accounts.models import User
from accounts.permissions import permission_required
from documents.access import visible_documents
from repairs.excel import workbook
from repairs.access import visible_jobs
from repairs.models import Job
from repairs.importer import read_book
from . import subcontracts as service
from .models import SubcontractRepresentative,SubcontractOrderAccess,SubcontractResult,SubcontractImportReceipt,LegalEntity,COMPANY_TAX_ID
from .access import visible_contracts,visible_objects

HEADERS={
 'companies':['ID компании','Компания','ИНН','Контакт','Примечание','Архив (0/1)','Версия'],
 'orders':['ID поручения','ID компании','ID контракта','ID объекта','Вид (repairs/valves/diagnostics/construction)','Название','Состав работ','План','Ед.','ID ответственного','Архив (0/1)','Версия'],
 'representatives':['ID представителя','ID компании','ID пользователя','Действует (0/1)','Версия'],
 'access':['ID доступа','ID поручения','ID представителя','Просмотр (0/1)','Результат (0/1)','Версия'],
 'documents':['ID связи','ID компании','ID поручения','ID документа','Назначение документа','Показывать (0/1)','Версия'],
 'repairs':['ID ремонта','ID поручения','Версия ремонта'],
 'results':['ID результата','ID поручения','Результат (complete/partial/problem)','Выполнено с начала','Дата','Пояснение'],
}
def kind_ok(kind):
    if kind not in HEADERS:raise Http404

def export_rows(user,kind):
    if kind=='companies':return [[str(c.pk),c.name,c.tax_id,c.contact,c.notes,int(c.archived),c.version] for c in service.companies(user).order_by('pk')]
    if kind=='orders':return [[str(o.pk),str(o.company_id),str(o.contract_id),str(o.project_object_id),o.kind,o.title,o.scope,str(o.planned_quantity) if o.planned_quantity is not None else '',o.unit,str(o.responsible_id or ''),int(o.archived),o.version] for o in service.orders(user,True).order_by('pk')]
    if kind=='representatives':return [[str(r.pk),str(r.company_id),str(r.user_id),int(r.active),r.version] for r in SubcontractRepresentative.objects.filter(company__in=service.companies(user)).order_by('pk')]
    if kind=='access':return [[str(g.pk),str(g.order_id),str(g.representative_id),int(g.active),int(g.can_report),g.version] for g in SubcontractOrderAccess.objects.filter(order__in=service.orders(user,True)).order_by('pk')]
    if kind=='documents':return [[str(f.pk),str(f.company_id),str(f.order_id or ''),str(f.document_id),f.label,int(f.active),f.version] for f in service.files(user).order_by('pk')]
    if kind=='repairs':return [[str(j.pk),str(j.subcontract_order_id),j.version] for j in visible_jobs(user).filter(subcontract_order__in=service.orders(user,True)).order_by('pk')]
    return [[str(r.pk),str(r.order_id),r.outcome,str(r.quantity) if r.quantity is not None else '',str(r.date),r.note] for r in SubcontractResult.objects.filter(order__in=service.orders(user,True)).order_by('pk')]

@permission_required('subcontract.manage')
def export(request,kind):
    kind_ok(kind);response=HttpResponse(human_xlsx.workbook(request.user,'subcontracts.'+kind,[] if request.GET.get('template')=='1' else export_rows(request.user,kind),HEADERS[kind]),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');response['Content-Disposition']=f'attachment; filename="armada-subcontract-{kind}.xlsx"';response['Cache-Control']='private, no-store';return response

def flag(v):
    if v not in ('0','1'):raise ValidationError('Флаг: 0 или 1.')
    return v=='1'

def apply_rows(user,kind,rows):
    actor=service.manager(user)
    LegalEntity.objects.select_for_update().get(tax_id=COMPANY_TAX_ID)
    # A single actor's imports serialize with their other administrative actions.
    # Lock all visible companies and orders in a stable order for multi-row files.
    list(service.companies(actor).select_for_update().order_by('pk'))
    list(service.orders(actor,True).select_for_update(no_key=True,of=('self',)).order_by('pk'))
    for row in rows:
        v=row['values']
        try:
            if kind=='companies':service.save_company(actor,uuid.UUID(v[0]) if v[0] else None,int(v[6]) if v[0] else None,dict(name=v[1],tax_id=v[2],contact=v[3],notes=v[4],archived=flag(v[5])))
            elif kind=='orders':
                service.save_order(actor,uuid.UUID(v[0]) if v[0] else None,int(v[11]) if v[0] else None,dict(company=service.companies(actor).get(pk=uuid.UUID(v[1])),contract=visible_contracts(actor).get(pk=uuid.UUID(v[2])),project_object=visible_objects(actor).get(pk=uuid.UUID(v[3])),kind=v[4],title=v[5],scope=v[6],planned_quantity=Decimal(v[7]) if v[7] else None,unit=v[8],responsible=User.objects.get(pk=uuid.UUID(v[9])) if v[9] else None,archived=flag(v[10])))
            elif kind=='representatives':service.save_representative(actor,uuid.UUID(v[1]),int(v[0]) if v[0] else None,int(v[4]) if v[0] else None,User.objects.get(pk=uuid.UUID(v[2])),flag(v[3]))
            elif kind=='access':
                if v[0] and not SubcontractOrderAccess.objects.filter(pk=int(v[0]),order_id=uuid.UUID(v[1]),representative_id=int(v[2])).exists():raise ValidationError('ID доступа не совпадает с поручением и представителем.')
                service.grant_order(actor,uuid.UUID(v[1]),int(v[2]),flag(v[3]),flag(v[4]),int(v[5]) if v[0] else None)
            elif kind=='documents':service.save_document(actor,uuid.UUID(v[1]),uuid.UUID(v[0]) if v[0] else None,int(v[6]) if v[0] else None,dict(order=service.orders(actor,True).get(pk=uuid.UUID(v[2])) if v[2] else None,document=visible_documents(actor).get(pk=uuid.UUID(v[3])),label=v[4],active=flag(v[5])))
            elif kind=='repairs':service.link_repairs(actor,uuid.UUID(v[1]),{str(uuid.UUID(v[0])):int(v[2])})
            elif v[0]:
                r=SubcontractResult.objects.filter(order__in=service.orders(actor,True)).get(pk=uuid.UUID(v[0]))
                expected=[str(r.pk),str(r.order_id),r.outcome,str(r.quantity) if r.quantity is not None else '',str(r.date),r.note]
                if v!=expected:raise ValidationError('Исходный результат не переписывается. Верните на доработку или исправьте и примите отдельной записью.')
            else:
                order=service.orders(actor).get(pk=uuid.UUID(v[1]));service.submit_result(actor,order.pk,dict(token=uuid.UUID(row['token']),expected_version=order.version,outcome=v[2],quantity=Decimal(v[3]) if v[3] else None,date=date.fromisoformat(v[4]),note=v[5],files=[]))
        except (ValidationError,ValueError,TypeError,InvalidOperation,ObjectDoesNotExist) as e:raise ValidationError(f'Строка {row["row"]}: '+('; '.join(e.messages) if isinstance(e,ValidationError) else 'Проверьте ID, версии и значения.')) from e

class UploadForm(forms.Form):file=forms.FileField(label='Книга XLSX',widget=forms.FileInput(attrs={'accept':'.xlsx'}))
def parse_rows(file,kind,user):
    headers=HEADERS[kind];book=human_xlsx.normalize(user,'subcontracts.'+kind,read_book(file),headers);cells=book['cells']
    if book['formulas']:raise ValidationError('Формулы не исполняются. Вставьте значения.')
    if [str(cells.get((1,i+1),'')).strip() for i in range(len(headers))]!=headers:raise ValidationError('Столбцы не совпадают с шаблоном.')
    rows=[];seen=set()
    for n in sorted({r for r,c in cells if r>1}):
        v=[str(cells.get((n,i+1),'')).strip() for i in range(len(headers))]
        if not any(v):continue
        if len(rows)>=500:raise ValidationError('За один импорт — до 500 строк.')
        if v[0] and v[0] in seen:raise ValidationError(f'Строка {n}: повтор ID.')
        if v[0]:seen.add(v[0])
        rows.append({'row':n,'values':v,'token':str(uuid.uuid4())})
    if not rows:raise ValidationError('Нет строк для переноса.')
    with transaction.atomic():apply_rows(user,kind,rows);transaction.set_rollback(True)
    return rows

@permission_required('subcontract.manage')
def upload(request,kind):
    kind_ok(kind);form=UploadForm(request.POST or None,request.FILES or None);rows=None;preview='';status=200
    if request.method=='POST':
        if request.POST.get('apply')=='1':
            try:
                payload=signing.loads(request.POST.get('preview',''),salt='subcontract-excel',max_age=3600)
                if payload['actor']!=str(request.user.pk) or payload['kind']!=kind:raise ValidationError('Чужой предварительный просмотр.')
                with transaction.atomic():
                    actor=service.manager(request.user)
                    receipt=SubcontractImportReceipt.objects.filter(token=payload['token']).first()
                    if not receipt:apply_rows(actor,kind,payload['rows']);SubcontractImportReceipt.objects.create(token=payload['token'],actor=actor)
                    elif receipt.actor_id!=actor.pk:raise ValidationError('Чужой импорт.')
            except (ValidationError,ValueError,KeyError,TypeError,signing.BadSignature) as e:form.add_error(None,'; '.join(e.messages) if isinstance(e,ValidationError) else 'Просмотр устарел или некорректен.');status=409
            else:return redirect('subcontracts')
        elif form.is_valid():
            try:
                rows=parse_rows(form.cleaned_data['file'],kind,request.user);preview=signing.dumps({'actor':str(request.user.pk),'kind':kind,'rows':rows,'token':str(uuid.uuid4())},salt='subcontract-excel',compress=True)
            except ValidationError as e:form.add_error('file',e)
    return render(request,'projects/subcontract_excel.html',{'form':form,'rows':rows,'preview':preview,'kind':kind,'headers':HEADERS[kind]},status=status)
