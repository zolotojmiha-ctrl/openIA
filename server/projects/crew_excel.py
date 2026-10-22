"""XLSX round-trip for crews, membership periods and place assignments."""
import uuid
from datetime import date
from django import forms
from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError,ObjectDoesNotExist
from django.db import transaction
from django.http import Http404,HttpResponse
from django.shortcuts import render,redirect
from django.utils import timezone
from accounts.permissions import permission_required
from repairs.excel import workbook
from repairs.importer import read_book
from documents.access import visible_people
from .models import BrigadeMember,CrewImportReceipt,Contract,Brigade
from .crews import visible_crews,visible_assignments,create_crew,update_crew,add_member,update_membership,assign_crew_place,close_assignment,manager

HEADERS={
 'brigades':['ID бригады','Название','ID мастера (персонал)','Мастер (справочно)','Начало участия нового мастера','В архиве (0/1)','Версия'],
 'members':['ID участия','ID бригады','ID сотрудника (персонал)','Сотрудник (справочно)','Начало участия','Окончание участия','Версия'],
 'assignments':['ID назначения','ID бригады','ID контракта','ID объекта','Начало работы','Окончание работы','Версия'],
}
def kind_or_404(kind):
    if kind not in HEADERS:raise Http404
    return kind

def export_rows(user,kind):
    if kind=='brigades':
        return [[str(c.pk),c.name,str(c.foreman_id),c.foreman.full_name,timezone.localdate().isoformat(),int(c.archived),c.version] for c in visible_crews(user).order_by('name')]
    if kind=='members':
        return [[str(m.pk),str(m.brigade_id),str(m.person_id),m.person.full_name,m.start_date.isoformat(),m.end_date.isoformat() if m.end_date else '',m.version] for m in BrigadeMember.objects.filter(brigade__in=visible_crews(user)).select_related('person').order_by('pk')]
    return [[str(a.pk),str(a.brigade_id),str(a.contract_id),str(a.project_object_id),a.start_date.isoformat(),a.end_date.isoformat() if a.end_date else '',a.version] for a in visible_assignments(user).order_by('pk')]

@permission_required('personnel.manage')
def export(request,kind):
    kind_or_404(kind);data=workbook([] if request.GET.get('template')=='1' else export_rows(request.user,kind),headers=HEADERS[kind])
    response=HttpResponse(data,content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');response['Content-Disposition']=f'attachment; filename="armada-{kind}.xlsx"';response['Cache-Control']='private, no-store';return response

class UploadForm(forms.Form):file=forms.FileField(label='Книга XLSX',widget=forms.FileInput(attrs={'accept':'.xlsx'}))

def parse_rows(file,kind,user):
    book=read_book(file)
    if book['formulas']:raise ValidationError('Формулы не поддерживаются. Вставьте значения.')
    cells=book['cells'];headers=HEADERS[kind]
    if [str(cells.get((1,i+1),'')).strip() for i in range(len(headers))]!=headers:raise ValidationError('Столбцы не совпадают с шаблоном выбранного реестра.')
    rows=[];seen=set()
    for number in sorted({r for r,c in cells if r>1}):
        values=[str(cells.get((number,i+1),'')).strip() for i in range(len(headers))]
        if not any(values):continue
        if len(rows)>=500:raise ValidationError('За один импорт — до 500 строк. Разделите файл.')
        if values[0] and values[0] in seen:raise ValidationError(f'Строка {number}: повтор ID.')
        if values[0]:seen.add(values[0])
        rows.append({'row':number,'values':values})
    if not rows:raise ValidationError('В файле нет данных.')
    # A rolled-back rehearsal validates the entire file and makes the preview concrete.
    # Audit and data writes are rolled back together; nothing leaves this transaction.
    try:
        with transaction.atomic():
            apply_rows(user,kind,rows)
            transaction.set_rollback(True)
    except (ValueError,KeyError,TypeError) as e:raise ValidationError('Проверьте ID, версии и даты в формате ГГГГ-ММ-ДД.') from e
    return rows

def _date(raw,required=True):
    if not raw and not required:return None
    try:return date.fromisoformat(raw)
    except ValueError:raise ValidationError('Дата должна быть в формате ГГГГ-ММ-ДД.')

def apply_rows(user,kind,rows):
    # Invoked in one outer transaction, so a bad later row cannot commit earlier rows.
    actor=manager(user)
    # Keep multi-row lock order independent of spreadsheet ordering.
    try:
        crew_ids={uuid.UUID(r['values'][0 if kind=='brigades' else 1]) for r in rows if r['values'][0 if kind=='brigades' else 1]}
        contract_ids={uuid.UUID(r['values'][2]) for r in rows} if kind=='assignments' else set()
    except (ValueError,IndexError) as e:raise ValidationError('Проверьте ID в ведомости.') from e
    list(Contract.objects.select_for_update().filter(pk__in=contract_ids).order_by('pk'))
    list(Brigade.objects.select_for_update().filter(pk__in=crew_ids).order_by('pk'))
    for row in rows:
        v=row['values']
        try:
            if kind=='brigades':
                person=visible_people(actor).filter(archived=False,pk=uuid.UUID(v[2])).first()
                if not person:raise ValidationError('Мастер не найден в доступном персонале.')
                if v[5] not in ('0','1'):raise ValidationError('Архив: укажите 0 или 1.')
                if v[0]:
                    crew=visible_crews(actor).filter(pk=uuid.UUID(v[0])).first()
                    if not crew:raise ValidationError('Неизвестный ID бригады. Для новой записи очистите ID.')
                    update_crew(actor,crew.pk,int(v[6]),v[1],person,v[5]=='1')
                else:
                    if v[5]=='1':raise ValidationError('Новая бригада создаётся действующей.')
                    create_crew(actor,v[1],person,[],_date(v[4]))
            elif kind=='members':
                crew=visible_crews(actor).filter(pk=uuid.UUID(v[1])).first();person=visible_people(actor).filter(pk=uuid.UUID(v[2]),archived=False).first()
                if not crew or not person:raise ValidationError('Проверьте ID бригады и сотрудника.')
                start,end=_date(v[4]),_date(v[5],False)
                if v[0]:
                    item=BrigadeMember.objects.filter(pk=int(v[0]),brigade=crew,person=person).first()
                    if not item:raise ValidationError('Участие не найдено. Перевод в другую бригаду оформляется новой строкой с пустым ID.')
                    update_membership(actor,item.pk,int(v[6]),start,end)
                else:add_member(actor,crew.pk,crew.version,person,start,end)
            else:
                start,end=_date(v[4]),_date(v[5],False)
                if v[0]:
                    assignment=visible_assignments(actor).filter(pk=uuid.UUID(v[0])).first()
                    if not assignment:raise ValidationError('Назначение недоступно.')
                    if [str(assignment.brigade_id),str(assignment.contract_id),str(assignment.project_object_id),assignment.start_date.isoformat()]!=v[1:5]:raise ValidationError('Место, контракт и начало существующего назначения не переписываются. Добавьте новое назначение с пустым ID.')
                    if assignment.version!=int(v[6]):raise ValidationError('Назначение изменилось. Выгрузите актуальный реестр.')
                    if assignment.end_date!=end:
                        if assignment.end_date or not end:raise ValidationError('Завершённый период не переписывается. Создайте новое назначение.')
                        close_assignment(actor,assignment.pk,assignment.version,end)
                else:
                    crew=visible_crews(actor).filter(pk=uuid.UUID(v[1])).first()
                    if not crew:raise ValidationError('Бригада не найдена.')
                    assign_crew_place(actor,crew.pk,crew.version,uuid.UUID(v[2]),uuid.UUID(v[3]),start,end)
        except (ValidationError,ValueError,TypeError,ObjectDoesNotExist) as e:
            detail=e.messages[0] if isinstance(e,ValidationError) else 'Проверьте ID, версии и даты.'
            raise ValidationError(f'Строка {row["row"]}: {detail}') from e

@permission_required('personnel.manage')
def upload(request,kind):
    kind_or_404(kind);form=UploadForm(request.POST or None,request.FILES or None);rows=None;token='';status=200
    if request.method=='POST':
        if request.POST.get('apply')=='1':
            try:
                payload=signing.loads(request.POST.get('preview',''),salt='crew-excel',max_age=3600)
                if payload['kind']!=kind or payload['actor']!=str(request.user.pk):raise ValidationError('Чужой или изменённый предварительный просмотр.')
                # Recheck every row/version/scope again at commit time.
                with transaction.atomic():
                    actor=manager(request.user)
                    receipt=CrewImportReceipt.objects.filter(token=payload['token']).first()
                    if receipt and receipt.actor_id!=actor.pk:raise ValidationError('Этот импорт принадлежит другому пользователю.')
                    if not receipt:
                        apply_rows(actor,kind,payload['rows'])
                        CrewImportReceipt.objects.create(token=payload['token'],actor=actor)
            except (signing.BadSignature,ValidationError,ValueError,KeyError) as e:form.add_error(None,e if isinstance(e,ValidationError) else 'Предварительный просмотр устарел.');status=409
            else:messages.success(request,'Excel перенесён. Изменения и прежние значения записаны в историю.');return redirect('brigades')
        elif form.is_valid():
            try:
                rows=parse_rows(form.cleaned_data['file'],kind,request.user)
                token=signing.dumps({'actor':str(request.user.pk),'kind':kind,'rows':rows,'token':str(uuid.uuid4())},salt='crew-excel',compress=True)
            except ValidationError as e:form.add_error('file',e)
    return render(request,'projects/crew_excel.html',{'form':form,'rows':rows,'preview':token,'headers':HEADERS[kind],'kind':kind},status=status)
