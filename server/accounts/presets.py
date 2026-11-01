PRESETS={
 'customer':{'label':'Заказчик: свои участки, ход работ, потребность и документы','permissions':[]},
 'technician':{'label':'Техник: учёт аренды и сверка счетов','permissions':['rental.manage','document.upload']},
 'pto':{'label':'ПТО: ведомости, дефекты, склады, нормы и документы','permissions':['works.prepare','repairs.prepare','stock.manage','document.upload','document.export','contract.changes']},
 'foreman':{'label':'Мастер: результаты и фото по назначенным работам','permissions':[]},
 'mechanic':{'label':'Механик: техника, водители и документы техники','permissions':['equipment.manage','document.upload']},
 'safety':{'label':'ОТ и ПБ: персонал, допуски и документы','permissions':['personnel.manage','document.upload','document.export']},
 'supply':{'label':'Снабжение: склады и движение материалов','permissions':['stock.manage']},
 'as_built':{'label':'Исполнительная документация: загрузка и реестры','permissions':['document.upload','document.export']},
}
