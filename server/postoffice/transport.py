"""TLS-only IMAP/SMTP. Passwords and message contents never enter application logs."""
import imaplib,smtplib,ssl,re
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from email.header import decode_header,make_header
from html.parser import HTMLParser
from django.conf import settings
from django.core.exceptions import ValidationError
from .crypto import reveal

MAX_MESSAGE=20*1024*1024
class Text(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.skip=0
    def handle_starttag(self,t,a):
        if t in ('script','style'):self.skip+=1
        if t in ('p','div','br','li'):self.parts.append('\n')
    def handle_endtag(self,t):
        if t in ('script','style'):self.skip=max(0,self.skip-1)
    def handle_data(self,v):
        if not self.skip:self.parts.append(v)

def decoded(value):
    try:return str(make_header(decode_header(str(value or ''))))[:2000]
    except (LookupError,UnicodeError):return str(value or '')[:2000]

@contextmanager
def connect(box):
    if box.state!='active' or not box.credential:raise ValidationError('Ящик ещё не подключён или доступ через ЛК отключён.')
    client=None
    try:
        client=imaplib.IMAP4_SSL(settings.ARMADA_IMAP_HOST,settings.ARMADA_IMAP_PORT,ssl_context=ssl.create_default_context(),timeout=15)
        client.login(box.address,reveal(box.credential));yield client
    except (OSError,imaplib.IMAP4.error,ValueError):raise ValidationError('Не удалось связаться с почтовым сервером. Проверьте подключение ящика и повторите позже.') from None
    finally:
        if client:
            try:client.logout()
            except (OSError,imaplib.IMAP4.error):pass

def checked(result):
    status,data=result
    if status!='OK':raise ValidationError('Почтовый сервер не выполнил операцию.')
    return data

def folders(client):
    rows=[]
    for raw in checked(client.list()):
        if not isinstance(raw,bytes):continue
        match=re.match(rb'\((.*?)\)\s+(?:"[^"]*"|NIL)\s+(.+)$',raw)
        if not match:continue
        flags=match[1].decode('ascii','ignore').lower()
        if '\\noselect' in flags:continue
        value=match[2].decode('ascii','replace').strip('"').replace('\\"','"').replace('\\\\','\\')
        if len(value)>250 or any(c in value for c in '\r\n'):continue
        label=value
        try:
            import base64
            label=re.sub(r'&([^-]*)-',lambda m:'&' if not m[1] else base64.b64decode(m[1].replace(',','/')+'='*((-len(m[1]))%4)).decode('utf-16-be'),value)
        except (ValueError,UnicodeError):pass
        rows.append({'name':value,'label':label,'sent':'\\sent' in flags,'trash':'\\trash' in flags})
    return rows

def select(client,folder,readonly=True):
    if folder not in [r['name'] for r in folders(client)]:raise ValidationError('Папка недоступна или была переименована.')
    return checked(client.select('"'+folder.replace('\\','\\\\').replace('"','\\"')+'"',readonly=readonly))

def listing(client,folder,page=1,search=''):
    select(client,folder)
    if search:
        if any(c in search for c in '\r\n') or len(search)>150:raise ValidationError('Проверьте поисковую строку.')
        # ASCII search works with all servers. Unicode search may require provider support.
        client.literal=search.encode('utf-8')
        ids=checked(client.uid('SEARCH','CHARSET','UTF-8','UNDELETED','TEXT'))[0].split()
    else:ids=checked(client.uid('SEARCH',None,'UNDELETED'))[0].split()
    ids=list(reversed(ids));rows=[]
    for uid in ids[(page-1)*25:page*25]:
        response=checked(client.uid('FETCH',uid,'(FLAGS BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE)])'))
        for part in response:
            if not isinstance(part,tuple):continue
            msg=BytesParser(policy=policy.default).parsebytes(part[1])
            rows.append({'uid':uid.decode(),'subject':decoded(msg['Subject']) or '(Без темы)','sender':decoded(msg['From']),'date':decoded(msg['Date']),'unread':b'\\Seen' not in part[0]});break
    return rows,len(ids)

def message(client,folder,uid):
    if not str(uid).isdigit() or int(uid)<1:raise ValidationError('Некорректный номер письма.')
    select(client,folder)
    sizes=checked(client.uid('FETCH',str(uid),'(RFC822.SIZE)'))
    size=next((int(m[1]) for x in sizes if isinstance(x,bytes) and (m:=re.search(rb'RFC822.SIZE (\d+)',x))),None)
    if size is None:raise ValidationError('Письмо не найдено.')
    if size>MAX_MESSAGE:raise ValidationError('Письмо больше 20 МБ. Откройте его через почтовое приложение.')
    data=checked(client.uid('FETCH',str(uid),'(BODY.PEEK[])'))
    raw=next((p[1] for p in data if isinstance(p,tuple)),None)
    if raw is None or len(raw)>MAX_MESSAGE:raise ValidationError('Письмо недоступно или превышает лимит.')
    msg=BytesParser(policy=policy.default).parsebytes(raw)
    part=msg.get_body(preferencelist=('plain','html'))
    body=''
    if part:
        try:body=part.get_content()
        except (LookupError,UnicodeError):body=part.get_payload(decode=True).decode('utf-8','replace')
        if part.get_content_type()=='text/html':
            text=Text();text.feed(body);body=''.join(text.parts)
    attachments=[]
    for index,p in enumerate(msg.walk()):
        if p.get_filename():attachments.append({'index':index,'name':decoded(p.get_filename()),'part':p})
    return {'subject':decoded(msg['Subject']),'sender':decoded(msg['From']),'to':decoded(msg['To']),'date':decoded(msg['Date']),'body':str(body)[:1000000],'attachments':attachments,'fingerprint':__import__('hashlib').sha256(raw).hexdigest(),'message_id':str(msg.get('Message-ID','')),'reply_to':str(msg.get('Reply-To') or msg.get('From') or '')}

def deliver(box,message,recipients):
    # A disconnect after DATA is ambiguous. Callers never automatically retry it.
    password=reveal(box.credential)
    context=ssl.create_default_context()
    if settings.ARMADA_SMTP_STARTTLS:
        smtp=smtplib.SMTP(settings.ARMADA_SMTP_HOST,settings.ARMADA_SMTP_PORT,timeout=20)
        try:smtp.ehlo();smtp.starttls(context=context);smtp.ehlo();smtp.login(box.address,password);result=smtp.send_message(message,from_addr=box.address,to_addrs=recipients)
        finally:
            try:smtp.quit()
            except (OSError,smtplib.SMTPException):pass
    else:
        with smtplib.SMTP_SSL(settings.ARMADA_SMTP_HOST,settings.ARMADA_SMTP_PORT,context=context,timeout=20) as smtp:
            smtp.login(box.address,password);result=smtp.send_message(message,from_addr=box.address,to_addrs=recipients)
    return result

def save_sent(box,payload):
    with connect(box) as client:
        name=next((f['name'] for f in folders(client) if f['sent']),None)
        if not name:raise ValidationError('На сервере не найдена папка отправленных.')
        checked(client.append('"'+name.replace('"','\\"')+'"','\\Seen',None,payload))
