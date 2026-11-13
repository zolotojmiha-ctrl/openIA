"""Trash retention starts on arrival/first discovery, never on the message's Date."""
from datetime import timedelta
import re
from django.core import signing
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction
from django.utils import timezone
from accounts.views import audit
from . import transport,services
from .access import boxes
from .models import Mailbox,TrashEntry,Letter,Attachment

SALT='mail-selection-v1'


def capabilities(client):
    return {value.decode('ascii').upper() if isinstance(value,bytes) else str(value).upper() for value in client.capabilities}


def validity(client):
    response=client.response('UIDVALIDITY')
    data=response[1] if isinstance(response,tuple) and len(response)==2 else None
    value=data[0].decode('ascii') if isinstance(data,list) and data and isinstance(data[0],bytes) else ''
    if not re.fullmatch(r'[1-9][0-9]{0,19}',value):
        raise ValidationError('Сервер не подтвердил идентификатор папки. Обновите список писем.')
    return value


def token(user,box,folder,generation,uids):
    return signing.dumps({'actor':str(user.pk),'session':user.session_version,'box':str(box.pk),
        'version':box.version,'folder':folder,'generation':generation,'uids':list(uids)},salt=SALT,compress=True)


def quoted(folder):
    return '"'+folder.replace('\\','\\\\').replace('"','\\"')+'"'


def snapshot(client,box,folder,now=None):
    transport.select(client,folder)
    generation=validity(client)
    ids={int(uid) for uid in transport.checked(client.uid('SEARCH',None,'ALL'))[0].split()}
    entries=TrashEntry.objects.filter(mailbox=box,folder=folder)
    entries.exclude(uidvalidity=generation).delete()
    known_rows=list(entries.values_list('pk','uid'))
    missing=[pk for pk,uid in known_rows if uid not in ids]
    for offset in range(0,len(missing),300):entries.filter(pk__in=missing[offset:offset+300]).delete()
    known={uid for _,uid in known_rows if uid in ids}
    stamp=now or timezone.now()
    TrashEntry.objects.bulk_create([TrashEntry(mailbox=box,folder=folder,uidvalidity=generation,uid=uid,trashed_at=stamp) for uid in ids-known],ignore_conflicts=True,batch_size=300)
    return generation,ids


@transaction.atomic
def apply_selection(user,box_id,selection,uids,action):
    try:data=signing.loads(selection,salt=SALT,max_age=1800)
    except signing.BadSignature:raise ValidationError('Выбор писем устарел. Обновите список и выберите письма заново.') from None
    fresh=services.actor(user)
    box=boxes(fresh).select_for_update(of=('self',)).filter(pk=box_id).first()
    if box is None:raise PermissionDenied
    if (data.get('actor')!=str(fresh.pk) or data.get('session')!=fresh.session_version or data.get('box')!=str(box.pk)
            or data.get('version')!=box.version):raise PermissionDenied
    selected=set(uids)
    if not selected or len(selected)>25 or not selected.issubset(set(data.get('uids',[]))) or any(not re.fullmatch(r'[1-9][0-9]{0,9}',uid) or int(uid)>4294967295 for uid in selected):
        raise ValidationError('Выберите от 1 до 25 писем на текущей странице.')
    folder=data['folder'];uidset=','.join(sorted(selected,key=int))
    if action not in ('trash','restore'):raise ValidationError('Неизвестное действие.')
    with transport.connect(box) as client:
        folders=transport.folders(client);source=next((f for f in folders if f['name']==folder),None)
        if not source or (action=='restore')!=bool(source.get('trash')):
            raise ValidationError('Восстановление доступно из корзины; удаление — из остальных папок.')
        target='INBOX' if action=='restore' else next((f['name'] for f in folders if f.get('trash')),None)
        if not target:raise ValidationError('На почтовом сервере не найдена папка «Корзина».')
        transport.select(client,folder,readonly=False)
        if validity(client)!=data['generation']:raise ValidationError('Папка изменилась. Обновите список: письма не перемещены.')
        present={uid.decode() for uid in transport.checked(client.uid('SEARCH',None,'UID',uidset))[0].split()}
        if selected!=present:raise ValidationError('Часть писем уже перемещена. Обновите список.')
        if 'MOVE' in capabilities(client):transport.checked(client.uid('MOVE',uidset,quoted(target)))
        elif 'UIDPLUS' in capabilities(client):
            transport.checked(client.uid('COPY',uidset,quoted(target)))
            transport.checked(client.uid('STORE',uidset,'+FLAGS.SILENT','(\\Deleted)'))
            transport.checked(client.uid('EXPUNGE',uidset))
        else:raise ValidationError('Сервер не поддерживает безопасное перемещение писем. Письма сохранены.')
        snapshot(client,box,target if action=='trash' else folder)
    audit(fresh,'mail_prepared',mailbox=str(box.pk),kind=action,uids=sorted(selected,key=int))
    return len(selected)


@transaction.atomic
def cleanup_box(box_id,now=None):
    now=now or timezone.now()
    box=Mailbox.objects.select_for_update().get(pk=box_id)
    if box.state!='active' or not box.credential:return 0
    count=0
    with transport.connect(box) as client:
        for folder in [f['name'] for f in transport.folders(client) if f.get('trash')]:
            generation,ids=snapshot(client,box,folder,now)
            due=list(TrashEntry.objects.filter(mailbox=box,folder=folder,uidvalidity=generation,
                trashed_at__lte=now-timedelta(days=30)).order_by('trashed_at','pk')[:500])
            if not due:continue
            if 'UIDPLUS' not in capabilities(client):
                raise ValidationError('Сервер не поддерживает адресное удаление. Корзина сохранена.')
            transport.select(client,folder,readonly=False)
            if validity(client)!=generation:raise ValidationError('Папка корзины изменилась. Очистка отложена.')
            uidset=','.join(str(row.uid) for row in due)
            # Remove the application's cached sent copy too, keeping only delivery audit metadata.
            from email import policy
            from email.parser import BytesParser
            response=transport.checked(client.uid('FETCH',uidset,'(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])'))
            message_ids=[]
            for part in response:
                if isinstance(part,tuple) and isinstance(part[1],bytes) and len(part[1])<=16384:
                    value=str(BytesParser(policy=policy.default).parsebytes(part[1]).get('Message-ID',''))
                    if re.fullmatch(r'<[^\r\n<>]{1,190}>',value):message_ids.append(value)
            cached=list(Letter.objects.filter(mailbox=box,message_id__in=message_ids,state='sent',purged_at__isnull=True))
            sent_folders=[f['name'] for f in transport.folders(client) if f.get('sent') and f['name']!=folder]
            purge_ids=[]
            for letter in cached:
                # A forged/duplicate Message-ID in incoming mail must not erase a live sent copy.
                remains=False
                for sent_folder in sent_folders:
                    transport.select(client,sent_folder)
                    if transport.checked(client.uid('SEARCH',None,'HEADER','Message-ID',quoted(letter.message_id)))[0].split():
                        remains=True;break
                if sent_folders and not remains:purge_ids.append(letter.pk)
            transport.select(client,folder,readonly=False)
            if validity(client)!=generation:raise ValidationError('Папка корзины изменилась. Очистка отложена.')
            transport.checked(client.uid('STORE',uidset,'+FLAGS.SILENT','(\\Deleted)'))
            # Never plain EXPUNGE: it would erase other clients' recently deleted mail.
            transport.checked(client.uid('EXPUNGE',uidset))
            cached=Letter.objects.filter(pk__in=purge_ids)
            Attachment.objects.filter(letter__in=cached).delete()
            cached.update(to='',cc='',bcc='',subject='',body='',in_reply_to='',error='',purged_at=now)
            TrashEntry.objects.filter(pk__in=[row.pk for row in due]).delete();count+=len(due)
            audit(None,'mail_prepared',kind='trash_expired',mailbox=str(box.pk),count=len(due),uids=[str(row.uid) for row in due])
    return count
