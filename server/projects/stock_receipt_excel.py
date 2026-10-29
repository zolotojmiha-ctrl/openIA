"""Simple new-stock worksheet: only material facts, no ledger IDs or versions."""
from django.core.exceptions import ValidationError
from accounts.xlsx import normal_date
from .models import StockItem

HEADERS=['Склад','Категория','Тип муфты','Диаметр, мм','Длина, мм','Стенка, мм','Количество','Единица','Дата получения','Откуда получено','Заводской №','Наименование','Марка электродов','Диаметр электрода, мм']


def parse(book):
    from . import stock_exchange as exchange
    if book['formulas']:raise ValidationError('Замените формулы значениями.')
    cells=book['cells'];columns={str(v).strip():c for (r,c),v in cells.items() if r==1 and str(v).strip()}
    if not {'Склад','Количество','Дата получения'}.issubset(columns):
        raise ValidationError('Нужны столбцы «Склад», «Количество» и «Дата получения».')
    unknown=set(columns)-set(HEADERS)
    if unknown:raise ValidationError('Неизвестные столбцы поступления: '+', '.join(sorted(unknown)))
    tables={name:[] for name in ('Остатки','Распределение','Изделия','Перемещения')}
    kinds={label.casefold():key for key,label in StockItem._meta.get_field('kind').choices}
    kinds['компазит']='composite'
    units=dict(StockItem._meta.get_field('unit').choices)
    for number in sorted({r for r,c in cells if r>1}):
        values={h:str(cells.get((number,c),'')).strip() for h,c in columns.items()}
        if not any(values.values()):continue
        try:
            get=lambda h:values.get(h,'')
            quantity=exchange.amount(get('Количество'),True if get('Тип муфты') else False)
            if quantity<=0:raise ValidationError('Количество должно быть больше нуля.')
            kind=kinds.get(get('Категория').casefold(),get('Категория'))
            if not kind and get('Тип муфты'):kind='coupling'
            if kind not in dict(StockItem._meta.get_field('kind').choices):
                raise ValidationError('Выберите категорию: муфта, композит, герметик, изоляция, электроды или материал.')
            method=get('Тип муфты');diameter=get('Диаметр, мм');length=get('Длина, мм');wall=get('Стенка, мм')
            unit=get('Единица') or units.get('m2' if kind=='insulation' else 'kg' if kind=='electrode' else 'pcs')
            name=get('Наименование')
            if not name:
                name=f'Муфта {method} Ø{diameter} × {length} мм, стенка {wall} мм' if kind=='coupling' else dict(StockItem._meta.get_field('kind').choices)[kind]
            key=f'НОВАЯ-{number}'
            row=[key,'',get('Склад'),name,dict(StockItem._meta.get_field('kind').choices)[kind],unit,method,diameter,length,wall,get('Марка электродов'),get('Диаметр электрода, мм'),'','','','',str(quantity),'нет',normal_date(get('Дата получения'),book['epoch']),get('Откуда получено'),'','','']
            if not get('Дата получения'):raise ValidationError('Укажите фактическую дату получения.')
            tables['Остатки'].append({'row':number,'values':row})
            if get('Заводской №'):
                if kind!='coupling' or quantity!=1:raise ValidationError('Заводской номер указывается для одной муфты. Для партии оставьте его пустым или внесите каждое изделие отдельной строкой.')
                tables['Изделия'].append({'row':number,'values':[key,'','',get('Заводской №'),'',get('Склад'),'1','','','','']})
        except ValidationError as exc:raise ValidationError(f'Поступление, строка {number}: '+'; '.join(exc.messages)) from exc
    if not tables['Остатки']:raise ValidationError('В файле нет поступлений. Заполните строки под заголовками.')
    if len(tables['Остатки'])>500:raise ValidationError('За один импорт — до 500 строк поступления.')
    return tables
