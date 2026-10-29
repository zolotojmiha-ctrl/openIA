"""Planned construction dimensions; actual warehouse pieces stay separate."""
import re
from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError

COUPLING_METHODS = ('p1', 'p2', 'p3', 'p4', 'p5u')


def metres(value):
    return format(Decimal(value).normalize(), 'f').replace('.', ',')


def planned_parts(length, composition):
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
        return total if total is not None else combined, parts
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError('Проверьте длину и состав: нужны длины в метрах, например 1,5 или 1+1,5.')
