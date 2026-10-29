from accounts import xlsx as human_xlsx
"""Spreadsheet editing submits requests, never approves or changes contract money."""
import uuid
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404,render,redirect
from repairs.excel import workbook
from repairs.importer import read_book
from .registry_excel import UploadForm,normalize_date
from .changes import scoped_contract,ChangeForm,submit,FIELDS,IMPACTS
from .models import ContractChange,ContractChangeSubmission,ContractChangeRevision
from .access import contract_card

HEADERS=['ID указания','Название','Получено','Применять с','ID объектов (через ;)','Работы и секции','Как было','Как должно быть','Вид основания','Основание','ID документа-основания','Влияние','Версия записи']
SALT='customer-change-excel'

def values(change):
    r=change.current_revision
    return [str(change.pk),r.title,r.received_on.isoformat(),r.effective_from.isoformat(),';'.join(r.object_ids),r.scope,r.before_text,r.after_text,r.get_basis_kind_display(),r.basis_note,str(r.basis_version.document_id) if r.basis_version_id else '', ';'.join(dict(IMPACTS)[v] for v in r.impacts),str(change.version)]

@login_required
def export(request,contract_id):
    contract=scoped_contract(request.user,contract_id)
    rows=[] if request.GET.get('template')=='1' else [values(c) for c in contract.customer_changes.select_related('current_revision__basis_version').order_by('pk')]
    # File IDs only if the file is actually accessible to the exporting actor.
    from documents.access import visible_documents
    docs={str(pk) for pk in visible_documents(request.user).values_list('pk',flat=True)}
    from .access import visible_objects
    object_ids={str(pk) for pk in visible_objects(request.user).values_list('pk',flat=True)}
    for row in rows:
        row[4]=';'.join(i for i in row[4].split(';') if i in object_ids)
        if row[10] not in docs:row[10]=''
    response=HttpResponse(human_xlsx.workbook(request.user,'changes',rows,HEADERS,sheet_name='Указания заказчика'),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition']='attachment; filename="armada-customer-changes.xlsx"';response['Cache-Control']='private, no-store'
    from accounts.views import audit
    audit(request.user,'document_exported',kind='customer_changes',contract=str(contract.pk),count=len(rows),format='xlsx')
    return response

def apply(user,contract_id,rows,token):
    with transaction.atomic():
        contract=scoped_contract(user,contract_id)
        for row in rows:
            v=row['values']
            try:
                change=get_object_or_404(contract.customer_changes,pk=uuid.UUID(v[0])) if v[0] else None
                data=dict(zip(FIELDS,[v[1],v[2],v[3],v[5],v[6],v[7],dict((label,key) for key,label in ContractChangeRevision._meta.get_field('basis_kind').choices).get(v[8],v[8]),v[9]]))
                data.update(token=uuid.uuid5(uuid.UUID(token),str(row['row'])),expected_version=v[12] if change else 0,objects=[x.strip() for x in v[4].split(';') if x.strip()],basis_document=v[10],impacts=[dict((label,key) for key,label in IMPACTS).get(x.strip(),x.strip()) for x in v[11].split(';') if x.strip()])
                form=ChangeForm(data,user=user,contract=contract,change=change)
                if not form.is_valid():raise ValidationError('; '.join(str(e) for errors in form.errors.values() for e in errors))
                prior=ContractChangeSubmission.objects.filter(token=data['token']).exists()
                if change and not prior:
                    original=values(change)
                    from documents.access import visible_documents
                    from .access import visible_objects
                    if original[10] and not visible_documents(user).filter(pk=original[10]).exists():original[10]=''
                    visible_ids={str(pk) for pk in visible_objects(user).values_list('pk',flat=True)}
                    original[4]=';'.join(i for i in original[4].split(';') if i in visible_ids)
                    if v==original:continue
                submit(user,contract.pk,change.pk if change else None,form.cleaned_data)
            except (ValidationError,ValueError,TypeError) as exc:
                raise ValidationError(f'Строка {row["row"]}: '+(exc.messages[0] if isinstance(exc,ValidationError) else 'Проверьте ID и версию.')) from exc

@login_required
def upload(request,contract_id):
    contract=scoped_contract(request.user,contract_id)
    from django.core.exceptions import PermissionDenied
    if contract.status=='archived':raise PermissionDenied
    form=UploadForm(request.POST or None,request.FILES or None);rows=None;preview=''
    if request.method=='POST':
        try:
            if request.POST.get('apply')=='1':
                payload=signing.loads(request.POST.get('preview',''),salt=SALT,max_age=3600)
                if payload['actor']!=str(request.user.pk) or payload['session']!=request.user.session_version or payload['contract']!=str(contract.pk):raise PermissionDenied
                apply(request.user,contract.pk,payload['rows'],payload['token'])
                messages.success(request,'Указания сохранены. Подтверждение и оформление договора выполняются отдельно.')
                return redirect('contract_changes',contract_id=contract.pk)
            elif form.is_valid():
                book=human_xlsx.normalize(request.user,'changes',read_book(form.cleaned_data['file']),HEADERS);cells=book['cells']
                if any(c>len(HEADERS) and str(v).strip() for (r,c),v in cells.items()):raise ValidationError('В файле есть лишние столбцы. Используйте актуальный шаблон.')
                if book['formulas'] or [str(cells.get((1,c+1),'')) for c in range(len(HEADERS))]!=HEADERS:raise ValidationError('Используйте шаблон реестра и значения без формул.')
                rows=[]
                for r in sorted({r for r,c in cells if r>1}):
                    v=[str(cells.get((r,c+1),'')).strip() for c in range(len(HEADERS))]
                    if not any(v):continue
                    if len(rows)>=500:raise ValidationError('До 500 строк за один перенос.')
                    v[2]=normalize_date(v[2],book['epoch']);v[3]=normalize_date(v[3],book['epoch'])
                    rows.append({'row':r,'values':v})
                if not rows:raise ValidationError('Нет заполненных строк.')
                ids=[str(uuid.UUID(r['values'][0])) for r in rows if r['values'][0]]
                if len(ids)!=len(set(ids)):raise ValidationError('ID указания повторяется.')
                token=str(uuid.uuid4())
                with transaction.atomic():apply(request.user,contract.pk,rows,token);transaction.set_rollback(True)
                preview=signing.dumps({'actor':str(request.user.pk),'session':request.user.session_version,'contract':str(contract.pk),'rows':rows,'token':token},salt=SALT,compress=True)
                if len(preview)>96*1024:raise ValidationError('Разделите файл на части: слишком большой объём пояснений.')
        except (ValidationError,ValueError,signing.BadSignature) as exc:
            rows=None;preview='';form.add_error(None,exc if isinstance(exc,ValidationError) else 'Предварительный просмотр устарел. Проверьте файл заново.')
    return render(request,'projects/change_excel.html',{'card':contract_card(contract,request.user),'form':form,'rows':rows,'preview':preview,'headers':HEADERS})
