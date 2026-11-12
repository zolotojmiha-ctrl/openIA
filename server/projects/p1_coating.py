"""Approved rounded PTO values. Never infer other sizes from grey columns."""
import json
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from django.core.exceptions import ValidationError

TABLE = json.loads(Path(__file__).with_name('p1_coating_table.json').read_text())


def dimensions(job, pieces=None):
    return sum(c.length_mm for c in pieces) if pieces else int(Decimal(job.length_m or 0) * 1000)


def row(diameter_mm, length_mm):
    if diameter_mm != TABLE['pipe_diameter_mm'] or str(length_mm) not in TABLE['rows']:
        raise ValidationError(f'В таблице ПТО нет нормы покрытия П1 для трубы Ø{diameter_mm or "?"}, длины {length_mm} мм. Подтверждены только 720/760 и табличные длины 1–8 м.')
    return dict(TABLE['rows'][str(length_mm)], length_mm=length_mm,
                length_m=Decimal(length_mm)/1000, pipe_diameter_mm=720, coupling_diameter_mm=760)


def coating(job, pieces=None):
    values = row(job.section.diameter_mm, dimensions(job, pieces))
    actual = getattr(job, 'actual_cleaning_length_m', None)
    extra = max(Decimal(0), Decimal(actual) - Decimal(values['cleaning_length_m'])) if actual is not None else Decimal(0)
    extra_area = extra * Decimal('0.720') * Decimal('3.14')
    measured=getattr(job,'actual_coating_length_m',None)
    if measured is not None:
        from .coating_measurements import extra_pipe_area
        extra_area=extra_pipe_area(job.section.diameter_mm,measured,values['cleaning_length_m'])

    rates = {'primer': Decimal('.21'), 'ksizol': Decimal('5.19'), 'wrapper': Decimal('.81')}
    return [{'kind': kind, 'name': name, 'quantity': str(Decimal(values[kind + '_kg']) + (extra_area * rates[kind]).quantize(Decimal('.001'), rounding=ROUND_CEILING)) if extra_area else values[kind + '_kg'], 'unit': 'кг',
             'source': TABLE['source'], 'norm_revision': TABLE['revision'],
             'length_mm': values['length_mm'], 'pipe_diameter_mm': 720, 'coupling_diameter_mm': 760,
             'area': str(Decimal(values['area_m2']) + extra_area), 'norm_quantity': values[kind + '_kg'], 'actual_coating_length_m':str(measured) if measured is not None else '', 'norm_coating_length_m':values['cleaning_length_m'], 'actual_cleaning_length_m': str(actual) if actual is not None else '', 'extra_area_m2': str(extra_area), 'cleaning_length_m': values['cleaning_length_m'],
             'cleaning_area_m2': values['cleaning_area_m2']}
            for kind, name in [('primer', 'Грунт / праймер'), ('ksizol', 'Ксизол'), ('wrapper', 'Обёртка Полилен')]]
