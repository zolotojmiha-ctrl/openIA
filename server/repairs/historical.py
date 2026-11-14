"""Actual legacy results with an explicitly unknown date, using existing records."""
from types import SimpleNamespace
from datetime import date
from .models import METHODS


def result(job, report=None):
    if report is not None:return report
    source=job.source if isinstance(job.source,dict) else {}
    if not source.get('historical_import') or job.state!='confirmed':return None
    try:performed=date.fromisoformat(source['completion_date']) if source.get('completion_date') else None
    except (ValueError,TypeError):performed=None
    method='' if source.get('actual_method_unknown') else job.method
    return SimpleNamespace(performed_date=performed,method=method,length_m=job.length_m,
        composition=job.composition,author=None,author_id=None,context={},historical=True,
        coupling_snapshot=source.get('component_snapshots',[]),
        get_method_display=lambda:dict(METHODS).get(method,'Не указан'))


def evidence_pages(job):
    """Bound source text for native MAX pages; preserve the original assertions."""
    source=job.source if isinstance(job.source,dict) else {}
    lines=[]
    def add(label,value):
        if value in (None,'',[],{}):return
        if isinstance(value,list):rendered='; '.join(str(v) for v in value)
        elif isinstance(value,dict):rendered=str(value.get('text') or 'Сведения сохранены в источнике')
        else:rendered=str(value)
        lines.append(label+': '+rendered)
    for member in source.get('source_members',[]):
        add('Источник',member.get('source_refs'))
        add('Основание статуса',member.get('evidence'))
        add('Примечания',member.get('notes'))
        add('Для проверки',member.get('review'))
        add('Исходная секция',member.get('original_pipe_section'))
    add('Исходная длина по ведомости, м',source.get('original_register_length_m'))
    add('Исходный состав',source.get('original_register_composition'))
    for part in source.get('component_snapshots',[]):
        add('Монтаж (не дата завершения ремонта)',part.get('mount_date'))
        add('Тип',part.get('method'));add('Длина части, мм',part.get('length_mm'))
        add('Стенка части, мм',part.get('wall_mm'));add('Заводской номер',part.get('serial_as_written'))
        add('Источник монтажа',str(part.get('source_sheet',''))+'!'+str(part.get('source_cell','')))
    text='\n'.join(lines) or 'Дополнительных исторических сведений нет.'
    return [text[i:i+2800] for i in range(0,len(text),2800)]
