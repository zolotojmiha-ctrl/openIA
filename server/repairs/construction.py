"""Planned construction dimensions; actual warehouse pieces stay separate."""
import re
from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError

COUPLING_METHODS = ('p1', 'p2', 'p3', 'p4', 'p5u')


def metres(value):
    return format(Decimal(value).normalize(), 'f').replace('.', ',')


def planned_parts(length, composition, method=None):
    """Return the total and individual lengths, deriving only a missing total."""
    text = (composition or '').strip().replace(',', '.')
    try:
        total = Decimal(str(length)) if length not in (None, '') else None
        if total is not None and (not total.is_finite() or not 0 < total < 1000000 or total * 1000 != (total * 1000).to_integral_value()):
            raise ValidationError('Укажите общую длину в метрах с точностью до миллиметра, например 1,5.')
        if text:
            if len(text) > 200 or not re.fullmatch(r'\d+(?:\.\d+)?(?:\s*\+\s*\d+(?:\.\d+)?)*', text):
                raise ValidationError('В составе укажите длины отдельных муфт через плюс, например 1+1,5. Для одной муфты оставьте состав пустым.')
            parts = [Decimal(p.strip()) for p in text.split('+')]
        elif total is not None:
            parts = [total]
        else:
            raise ValidationError('Укажите требуемую общую длину. Для нескольких муфт можно заполнить только состав, например 1+1,5.')
        if any(not 0 < p < 1000000 or p * 1000 != (p * 1000).to_integral_value() for p in parts):
            raise ValidationError('Каждая длина в составе должна быть больше нуля и указана с точностью до миллиметра.')
        combined = sum(parts)
        if combined >= 1000000:
            raise ValidationError('Общая длина конструкции должна быть меньше 1000000 м.')
        if total is not None and combined != total:
            raise ValidationError(f'В карточке дефекта длина {metres(total)} м, а сумма состава {metres(combined)} м. Уточните плановую длину или состав в карточке дефекта.')
        if method == 'p2' and len(parts) > 1:
            raise ValidationError('П2 нельзя стыковать: нужна одна цельная муфта.')
        if method == 'p1' and combined > Decimal('10.5'):
            raise ValidationError('Рабочая максимальная длина П1 — 10,5 м.')
        return total if total is not None else combined, parts
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError('Проверьте длину и состав: нужны длины в метрах, например 1,5 или 1+1,5.')


def demand_parts(length,composition='',method=None):
    """Canonical supply plan; actual installed pieces and source stay unchanged."""
    total,old_parts=planned_parts(length,composition,method)
    if method not in ('p1','p2'):return total,old_parts
    millimetres=int(total*1000)
    if method=='p2':
        if millimetres>1500:raise ValidationError('Для П2 длиннее 1500 мм нет типовой муфты в плановом перечне. Уточните конструкцию.')
        return total,[Decimal(1000 if millimetres<=1000 else 1500)/1000]
    # Minimise excess length, then joins, then use of rare 2500 mm pieces.
    target=max(1000,((millimetres+499)//500)*500)
    best={0:()}
    def rank(parts):return (len(parts),parts.count(2500),tuple(-n for n in sorted(parts,reverse=True)))
    for value in range(500,target+1,500):
        choices=[tuple(sorted(best[value-size]+(size,),reverse=True)) for size in (1000,1500,2000,2500) if value-size in best]
        if choices:best[value]=min(choices,key=rank)
    if target not in best:raise ValidationError('Не удалось подобрать типовые муфты для этой длины П1.')
    return total,[Decimal(n)/1000 for n in best[target]]
