"""Installation facts collected by report forms under the stock/job locks."""
from decimal import Decimal
from django.core.exceptions import ValidationError
from .models import Coupling,CouplingUse


def apply(actor,job,facts,selected):
    if not isinstance(facts,dict):raise ValidationError('Проверьте сведения об установленной муфте.')
    from projects.models import StockItem
    from projects.stock import prepared_couplings
    prepared=prepared_couplings(job)
    if facts.get('kind')=='reserved':
        serials=facts.get('serials',{})
        if set(serials)!={str(c.pk) for c in prepared}:raise ValidationError('Закрепление муфт изменилось. Откройте форму заново.')
        by_id={str(c.pk):c for c in selected}
        if not set(serials).issubset(by_id):raise ValidationError('Проверьте состав закреплённых муфт.')
        # Clear changed numbers first so a checked correction can swap labels.
        changes=[]
        for c in prepared:
            serial=str(serials[str(c.pk)]).strip()
            if not serial or len(serial)>100:raise ValidationError('Укажите заводской номер каждой закреплённой муфты, до 100 символов.')
            changes.append((c,serial))
        if len({serial for _,serial in changes})!=len(changes):raise ValidationError('Заводские номера муфт не должны повторяться.')
        for c,serial in changes:
            if c.serial!=serial:
                if CouplingUse.objects.filter(coupling=c).exists():raise ValidationError('Заводской номер установленной муфты меняется через исправление результата.')
                c.serial='';c.save(update_fields=['serial'])
                StockItem.objects.filter(coupling=c).update(serial='')
        for c,serial in changes:
            if StockItem.objects.filter(legal_entity=job.section.project_object.legal_entity,method=c.method,serial=serial,deleted_at__isnull=True).exclude(coupling=c).exists():raise ValidationError('Муфта этого типа с таким заводским номером уже числится в учёте.')
            c.serial=serial;c.full_clean();c.save(update_fields=['serial']);by_id[str(c.pk)].serial=serial
            from django.db.models import F
            StockItem.objects.filter(coupling=c).update(serial=serial,version=F('version')+1)
        return list(by_id.values())
    if facts.get('kind')!='manual' or prepared:raise ValidationError('На работу закреплены складские муфты. Используйте их размеры и заводские номера.')
    pieces=facts.get('pieces',[])
    if not isinstance(pieces,list) or not 1<=len(pieces)<=30:raise ValidationError('Укажите от 1 до 30 установленных муфт.')
    result=[]
    for piece in pieces:
        serial=str(piece.get('serial','')).strip();length=piece.get('length_mm')
        if not serial or len(serial)>100 or not isinstance(length,int) or length<=0:raise ValidationError('Для каждой муфты нужны фактическая длина и заводской номер.')
        c=Coupling.objects.filter(project_object=job.section.project_object,method=job.method,serial=serial).first()
        if c:
            if actor.role=='subcontractor' and not CouplingUse.objects.filter(coupling=c,job=job).exists():raise ValidationError('Этот заводской номер уже есть в реестре. Попросите ответственного закрепить муфту за этой работой.')
            if c.length_mm!=length:raise ValidationError('Этот заводской номер уже относится к муфте другой длины. Проверьте сведения.')
            if CouplingUse.objects.filter(coupling=c).exclude(job=job).exists():raise ValidationError('Муфта с этим заводским номером уже установлена на другой работе.')
        else:
            if StockItem.objects.filter(legal_entity=job.section.project_object.legal_entity,method=job.method,serial=serial,deleted_at__isnull=True).exists():raise ValidationError('Этот заводской номер уже числится на складе. Сначала закрепите складскую муфту за дефектом.')
            c=Coupling(project_object=job.section.project_object,method=job.method,length_mm=length,wall_mm=job.wall_mm,serial=serial,source={'reported_without_stock':True})
            c.full_clean();c.save()
        if c.pk in {item.pk for item in result}:raise ValidationError('Заводские номера муфт не должны повторяться.')
        result.append(c)
    return result
