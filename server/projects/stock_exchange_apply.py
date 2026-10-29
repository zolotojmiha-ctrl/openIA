"""Transactional command plan for stock spreadsheet round trips."""
import uuid
from collections import defaultdict
from decimal import Decimal
from django.core.exceptions import ValidationError,ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from . import stock,stock_groups as groups,stock_planning as planning
from .models import StockItem,StockImportReceipt,StockMovement

@transaction.atomic
def apply(user,tables,token=None,dry=False,source_label='Excel'):
    from . import stock_exchange as x
    actor=stock.manager(user);stock.lock()
    if token:
        receipt=StockImportReceipt.objects.filter(token=token).first()
        if receipt:
            if receipt.actor_id!=actor.pk:raise ValidationError('Чужое подтверждение.')
            return []
    initial=groups.groups(actor,include_archived=True);plans={};targets=defaultdict(lambda:defaultdict(int));changes=[];pieces=[];extras=[];metadata={};renames={};new_ids={};whole_moves=[];deliveries=[]
    work=x.work_choices(actor)
    def warehouse(name):
        rows=list(stock.warehouses(actor).filter(name=name,archived=False))
        if len(rows)!=1:raise ValidationError('Склад не найден, недоступен или название неоднозначно: '+name)
        return rows[0]
    def fresh(g):return next((r for r in groups.groups(actor,include_archived=True) if r['id']==g['id']),g)
    def job(label,g=None):
        found=[j for j,text in work if text==label or planning.job_label(j)==label]
        found={j.pk:j for j in found}
        if len(found)==1:return next(iter(found.values()))
        old=[d['job'] for d in (g['distribution'] if g else []) if d['state']=='reserved' and (x.closed_label(d['job'])==label or d['label']==label)]
        if len(old)==1:return old[0]
        raise ValidationError('Работа не найдена или неоднозначна. Скопируйте значение с листа «Работы».')
    def get_data(r):
        kind=next((k for k,v in x.KIND.items() if v.casefold()==r[4].casefold()),None)
        unit=next((k for k,v in x.UNIT.items() if v.casefold()==r[5].casefold()),None) or {'шт':'pcs','pcs':'pcs','м2':'m2','m2':'m2','кв.м':'m2','kg':'kg'}.get(r[5].casefold())
        if not kind or not unit:raise ValidationError('Неизвестная категория / единица.')
        def mm(value):
            if not value:return None
            n=x.numeric(value)
            if n is None or n<=0 or n!=n.to_integral_value() or n>100000:raise ValidationError('Размер нужен положительным целым числом в мм.')
            return int(n)
        return dict(sku='',name=r[3],kind=kind,unit=unit,method=x.method(r[6]) or '',diameter_mm=mm(r[7]),length_mm=mm(r[8]),wall_mm=mm(r[9]),electrode_brand=r[10],electrode_diameter=x.numeric(r[11]) if r[11] else None,archived=False,serial='',receipt_origin=r[19])
    # Validate every original snapshot before making any changes.
    for entry in tables['Остатки']:
        r=list(entry['values'])+['']*max(0,len(x.HEADERS['Остатки'])-len(entry['values']));key=r[0]
        try:
            if not key or key in plans:raise ValidationError('Нужен уникальный код строки.')
            if r[17].casefold() not in ('да','нет'):raise ValidationError('Обновить распределение: да или нет.')
            if not r[3] or len(r[3])>200:raise ValidationError('Наименование: от 1 до 200 символов.')
            g=groups.current(actor,r[1],initial) if r[1] else None
            if g:
                if g['id']!=key:raise ValidationError('Версия не соответствует коду строки.')
                x.validate_existing(r,g)
            elif not key.startswith('НОВАЯ-'):raise ValidationError('Новая строка должна иметь код НОВАЯ-1 и т.д.')
            dest=g['warehouse'] if g and g['warehouse'] and r[2]==g['warehouse'].name else (warehouse(r[2]) if r[2] else None)
            target=x.amount(r[16],g['item'].kind=='coupling' if g else r[4].casefold()=='муфта')
            if (not g or not g['warehouse']) and target>0 and not dest:raise ValidationError('Укажите склад для поступления.')
            if not g and target<=0:raise ValidationError('Для новой позиции нужно положительное количество.')
            delivery=x.amount(r[20] or '0',g['item'].kind=='coupling' if g else r[4].casefold()=='муфта')
            if delivery and not g:raise ValidationError('В новой строке используйте «Новый остаток», без дополнительного поступления.')
            if delivery and not dest:raise ValidationError('Укажите склад поступления.')
            plans[key]={'r':r,'row':entry['row'],'g':g,'target':target,'dest':dest,'delivery':delivery,'changed_pieces':False}
        except ValidationError as error:raise ValidationError(f'Остатки, строка {entry["row"]}: '+ '; '.join(error.messages))
    if not plans:raise ValidationError('Лист «Остатки» пуст.')
    for entry in tables['Распределение']:
        r=entry['values'];key=r[0]
        try:
            if key not in plans:raise ValidationError('Нет кода строки на листе остатков.')
            p=plans[key]
            if p['r'][17].casefold()!='да':raise ValidationError('Включите обновление распределения для этой строки.')
            q=x.amount(r[2],True)
            if q:targets[key][str(job(r[1],p['g']).pk)]+=int(q)
        except ValidationError as error:raise ValidationError(f'Распределение, строка {entry["row"]}: '+ '; '.join(error.messages))
    seen=set()
    for entry in tables['Изделия']:
        r=list(entry['values'])+['']*max(0,len(x.HEADERS['Изделия'])-len(entry['values']));key=r[0]
        try:
            if key not in plans:raise ValidationError('Нет кода строки на листе остатков.')
            p=plans[key];g=p['g']
            if not r[1]:
                if (g and g['item'].kind!='coupling' or not g and p['r'][4].casefold()!='муфта') or x.amount(r[6] or '1',True)!=1 or not r[3] or len(r[3])>100 or len(r[8])>200:raise ValidationError('Дополнительный номер: одна муфта, номер до 100 символов, количество 1.')
                if any(r[c] for c in [2,4,7,9,10]):raise ValidationError('В новой строке номера оставьте версию, состояние, даты и назначение пустыми.')
                extras.append((key,r[3],r[8]));continue
            m=next((m for m in g['members'] if str(m['item'].pk)==r[1]),None) if g else None
            if not m or (key,r[1]) in seen:raise ValidationError('Неизвестная или повторная позиция.')
            seen.add((key,r[1]));item=m['item']
            if str(item.version)!=r[2] or m['label']!=r[4] or m['quantity']!=x.amount(r[6]) or str(m['receipt_date'] or '')!=r[7] or str(m['installed_date'] or '')!=r[10]:raise ValidationError('Не меняйте ID, версию, состояние, количество или даты изделия.')
            if len(r[3])>100 or len(r[8])>200:raise ValidationError('Слишком длинный номер / источник.')
            if r[3]!=m['serial'] and item.kind!='coupling':raise ValidationError('Заводской номер заполняется для муфты.')
            if r[3]!=m['serial'] or r[8]!=m['receipt_source']:
                if item.pk in metadata and metadata[item.pk]!=(r[3],r[8]):raise ValidationError('Для одной карточки разные номера / источники.')
                metadata[item.pk]=(r[3],r[8])
            old_labels={m['job_label'],x.closed_label(m['job']),dict((str(j.pk),label) for j,label in work).get(str(m['job'].pk),'')}- {''} if m['job'] else {''}
            label_unchanged=r[9] in old_labels
            assignment=None if label_unchanged else (job(r[9],g) if r[9] else False)
            source=g['warehouse'];dest=source if source and r[5]==source.name else (warehouse(r[5]) if r[5] else None)
            moved=source and dest and source.pk!=dest.pk
            if source and not dest:raise ValidationError('Для перемещения укажите склад получателя.')
            if m['state']=='installed' and (assignment is not None or moved):raise ValidationError('Смонтированное изделие меняется через исправление результата ремонта.')
            if assignment is not None:
                if item.kind!='coupling' or not (item.individual or item.coupling_id or sum(b.quantity for b in item.balances.all())==1 or not g['warehouse'] and len(g['members'])==1 and p['target']==1):raise ValidationError('Для назначения партии используйте количества на листе «Распределение».')
                p['changed_pieces']=True
            if assignment is not None or moved:pieces.append({'key':key,'item_id':item.pk,'job':assignment,'dest':dest if moved else None,'quantity':m['quantity'],'source':source})
        except ValidationError as error:raise ValidationError(f'Изделия, строка {entry["row"]}: '+ '; '.join(error.messages))
    transfers=[]
    for entry in tables.get('Перемещения',[]):
        r=entry['values'];key=r[0]
        try:
            if key not in plans or not plans[key]['g'] or not plans[key]['g']['warehouse']:raise ValidationError('Для перемещения нужна существующая строка с остатком.')
            if r[4].casefold() not in ('да','нет'):raise ValidationError('Перемещать резервы: да или нет.')
            transfers.append((key,warehouse(r[1]),x.amount(r[2],plans[key]['g']['item'].kind=='coupling'),x.date_value(r[3]),r[4].casefold()=='да',r[5] or 'Перемещение из Excel'))
        except ValidationError as error:raise ValidationError(f'Перемещения, строка {entry["row"]}: '+ '; '.join(error.messages))
    for key,p in plans.items():
        g=p['g'];existing={str(d['job'].pk):int(d['quantity']) for d in g['distribution'] if d['state']=='reserved'} if g else {}
        if p['changed_pieces'] and p['r'][17].casefold()=='да' and dict(targets[key])!=existing:raise ValidationError('Одновременно изменены количества распределения и назначения отдельных муфт. Выберите один способ правки для строки '+p['r'][3])
        if g and g['warehouse'] and p['dest'] and p['dest'].pk!=g['warehouse'].pk and (any(t[0]==key for t in transfers) or any(c['key']==key and c['dest'] for c in pieces)):raise ValidationError('Не смешивайте перенос всей строки со складом отдельных изделий / частичным перемещением.')
    # All validation is complete. Lock remains held through the whole command set.
    # Explicit individual changes release old reservations before swaps/moves.
    for c in pieces:
        item=stock.items(actor).get(pk=c['item_id'])
        if c['job'] is not None and item.reserved_job_id:planning.reserve(actor,item.pk,item.version,None)
    for key,p in sorted(plans.items(),key=lambda kv:kv[1]['g'] is None):
        r=p['r'];g=fresh(p['g']) if p['g'] else None;dest=p['dest'];target=p['target']
        try:
            if g is None or not g['warehouse']:
                if target:
                    data=get_data(r) if not g else groups.values(g['item'])
                    data.update(name=r[3],receipt_origin=r[19])
                    if g and len(g['members'])==1 and not g['item'].movements.exists() and not g['item'].coupling_id:
                        data.update(catalogue_item=g['item'],catalogue_version=g['item'].version)
                    probe=StockItem(**{k:v for k,v in data.items() if k in [*stock.ITEM_FIELDS,'electrode_brand','electrode_diameter']})
                    gid=groups.identity(probe,dest.pk)
                    before=next((xg for xg in groups.groups(actor) if xg['id']==gid),None);before_ids={m['item'].pk for m in before['members']} if before else set()
                    planning.receive(actor,data,dest,target,x.date_value(r[18] or str(timezone.localdate())), 'Поступление из '+source_label,uuid.uuid4())
                    g=next(xg for xg in groups.groups(actor) if xg['id']==gid);new_ids[key]={m['item'].pk for m in g['members']}-before_ids
                    changes.append(f'{r[3]}: поступление {target} {r[5]} на {dest.name}, дата {r[18] or timezone.localdate()}, источник {r[19] or "—"}')
                    if r[17].casefold()=='да':
                        combined=defaultdict(int,{str(d['job'].pk):int(d['quantity']) for d in g['distribution'] if d['state']=='reserved'})
                        for jid,n in targets[key].items():combined[jid]+=n
                        groups.distribute(actor,g['token'],combined);g=fresh(g)
                if g and not target and r[3]!=g['item'].name:
                    for m in g['members']:renames[m['item'].pk]=r[3]
                p['g']=g
                continue
            delta=target-p['g']['quantity'];do_bulk=r[17].casefold()=='да' and not p['changed_pieces']
            old_alloc={str(d['job'].pk):int(d['quantity']) for d in p['g']['distribution'] if d['state']=='reserved'}
            moved=dest and dest.pk!=g['warehouse'].pk
            if do_bulk and g['item'].kind=='coupling' and (delta<0 or moved and dict(targets[key])!=old_alloc):
                groups.distribute(actor,g['token'],{} if moved else targets[key]);g=fresh(g)
            if delta:
                groups.correct_quantity(actor,g['token'],g['quantity']+delta,note='Корректировка остатка из '+source_label);changes.append(f'{r[3]} · {g["warehouse"].name}: остаток {g["quantity"]} → {target}');g=fresh(g)
            if moved and target:
                whole_moves.append((key,g['id'],{m['item'].pk for m in g['members'] if m['quantity']>0},dest,target,dict(targets[key]) if do_bulk and dict(targets[key])!=old_alloc else None))
            elif do_bulk:
                if g['item'].kind!='coupling':raise ValidationError('Распределение по дефектам — только для муфт.')
                if target or targets[key]:groups.distribute(actor,g['token'],targets[key]);g=fresh(g)
                changes.append(f'{r[3]}: распределение {sum(targets[key].values())} шт.')
            if p['delivery']:deliveries.append((key,dest,p['delivery'],r[21],r[22]))
            if r[3]!=g['item'].name:
                for m in g['members']:
                    if m['item'].pk in renames and renames[m['item'].pk]!=r[3]:raise ValidationError('Разные наименования одной карточки.')
                    renames[m['item'].pk]=r[3]
                changes.append('Наименование → '+r[3])
            p['g']=g
        except (ValidationError,ObjectDoesNotExist) as error:raise ValidationError(f'Остатки, строка {p["row"]} ({r[3]}): '+('; '.join(error.messages) if isinstance(error,ValidationError) else 'Запись недоступна.'))
    # Move only after all baseline corrections/distributions, so an incoming
    # transfer is never subtracted again by an unchanged destination row.
    for key,source_id,ids,dest,q,assign in whole_moves:
        g=next(g for g in groups.groups(actor) if g['id']==source_id)
        moved_ids=groups.transfer(actor,g['token'],dest.pk,q,timezone.localdate(),'Перенос строки из Excel',include_reserved=True,item_ids=ids)
        target_id=groups.identity(g['item'],dest.pk);g=next(g for g in groups.groups(actor) if g['id']==target_id)
        if assign is not None:groups.distribute(actor,g['token'],assign,item_ids=moved_ids);g=fresh(g)
        plans[key]['g']=g;changes.append(f'{g["item"].name}: переместить {q} на {dest.name}')
    for key,dest,q,date,origin in deliveries:
        g=fresh(plans[key]['g']);data=groups.values(g['item']);data.update(name=plans[key]['r'][3],serial='',receipt_origin=origin)
        planning.receive(actor,data,dest,q,x.date_value(date),'Новое поступление из Excel',uuid.uuid4())
        plans[key]['g']=fresh(g);changes.append(f'{g["item"].name}: новая поставка {q}, дата {date}, источник {origin or "—"}')
    for key,dest,q,date,reserved,note in transfers:
        g=fresh(plans[key]['g']);groups.transfer(actor,g['token'],dest.pk,q,date,note,include_reserved=reserved)
        changes.append(f'{g["item"].name}: переместить {q} с {g["warehouse"].name} на {dest.name}')
    for c in pieces:
        item=stock.items(actor).get(pk=c['item_id'])
        if c['dest']:
            stock.move(actor,item.pk,'transfer',c['quantity'],c['source'].pk,c['dest'].pk,timezone.localdate(),'Перемещение позиции из Excel',uuid.uuid4(),allow_holders=True)
            changes.append(f'{item.name}: на склад {c["dest"].name}')
    for c in pieces:
        if c['job'] is not None:
            item=stock.items(actor).get(pk=c['item_id']);planning.reserve(actor,item.pk,item.version,c['job'].pk if c['job'] else None)
            changes.append(f'Муфта № {item.serial or "—"}: назначение → {planning.job_label(c["job"]) if c["job"] else "Свободна"}')
    for pk,name in renames.items():
        item=stock.items(actor).get(pk=pk);data=groups.values(item);data.update(name=name,coupling=item.coupling);stock.save_item(actor,pk,item.version,data)
    for pk,(serial,origin) in metadata.items():
        item=stock.items(actor).get(pk=pk);data=groups.values(item);data.update(serial=serial,receipt_origin=origin,coupling=item.coupling);stock.save_item(actor,pk,item.version,data);changes.append(f'{item.name}: номер / источник → {serial or "—"} / {origin or "—"}')
    used=set()
    for key,serial,origin in extras:
        g=fresh(plans[key]['g'])
        choices=[m for m in g['members'] if m['quantity']>0 and not m['serial'] and m['item'].pk not in used and (key not in new_ids or m['item'].pk in new_ids[key])]
        if not choices:raise ValidationError('Не хватает изделий без номера для '+serial)
        m=choices[0];item=stock.items(actor).get(pk=m['item'].pk)
        if not item.individual and not item.coupling_id and (m['quantity']>1 or item.balances.filter(quantity__gt=0).count()>1):item=planning.split(actor,item.pk,item.version,g['warehouse'].pk)
        data=groups.values(item);data.update(serial=serial,receipt_origin=origin or item.receipt_origin,coupling=item.coupling);stock.save_item(actor,item.pk,item.version,data);used.add(item.pk);changes.append('Заводской № '+serial)
    if token:StockImportReceipt.objects.create(token=token,actor=actor)
    if dry:transaction.set_rollback(True)
    return changes
