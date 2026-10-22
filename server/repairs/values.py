"""Input rules for defect registers; evidence in old reports stays unchanged."""
import re
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
from django.core.exceptions import ValidationError


def kilometre(value):
    try:
        text=str(value).strip().replace(',','.')
        if len(text)>512:raise InvalidOperation
        number=Decimal(text)
        if not number.is_finite() or not 0<=number<1000000:raise InvalidOperation
        result=number.quantize(Decimal('.001'),rounding=ROUND_HALF_UP)
        if result>=1000000:raise InvalidOperation
        return result
    except (InvalidOperation,ValueError,TypeError):
        raise ValidationError('некорректный километр. Укажите число от 0 до 999999.999. Значение округляется до трёх знаков после запятой.')


def identifier(value,label,optional=False):
    text=str(value).strip()
    if optional and not text:return ''
    if not re.fullmatch(r'[0-9]{1,80}',text):
        raise ValidationError(f'{label}: допускаются только цифры, до 80 знаков. Начальные нули сохраняются.')
    return text


def matching_defects(section,km,number,contract=None):
    from .models import Defect
    rounded=kilometre(km)
    return Defect.objects.filter(contract=contract,section=section,number=number,kilometre__gte=max(Decimal(0),rounded-Decimal('.0005')),kilometre__lt=rounded+Decimal('.0005'))


_parse_kilometre=kilometre

def get_or_create_defect(section,kilometre,number,defaults,contract=None):
    from .models import Defect
    found=list(matching_defects(section,kilometre,number,contract).order_by('pk')[:2])
    if len(found)>1:raise ValidationError('После округления найдено несколько существующих записей этого дефекта. Проверьте дубли перед импортом.')
    if found:return found[0],False
    return Defect.objects.get_or_create(contract=contract,section=section,kilometre=_parse_kilometre(kilometre),number=number,defaults=defaults)
