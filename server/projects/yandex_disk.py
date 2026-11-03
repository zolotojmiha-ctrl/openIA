"""Private app-folder Disk API. OAuth is sent only to the fixed API origin."""
import hashlib,json,ssl
from pathlib import Path
from django.views.decorators.debug import sensitive_variables
from urllib.error import HTTPError,URLError
from urllib.parse import urlencode,urlsplit
from urllib.request import Request,build_opener,HTTPSHandler,HTTPRedirectHandler

API='https://cloud-api.yandex.net/v1/disk'
ROOT='app:/armada'
class DiskError(Exception):pass

def signed_url(url):
    try:
        parts=urlsplit(url);host=parts.hostname or ''
        if parts.scheme!='https' or parts.username or parts.password or parts.port not in (None,443):raise ValueError
        if not (host.endswith('.disk.yandex.net') or host.endswith('.disk.yandex.ru')):raise ValueError
    except (ValueError,TypeError):raise DiskError('Недопустимый адрес передачи файла.') from None
    return url

class Redirects(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if req.has_header('Authorization'):raise DiskError('Перенаправление API отклонено.')
        signed_url(newurl)
        result=super().redirect_request(req,fp,code,msg,headers,newurl)
        if result:result.remove_header('Authorization')
        return result

def app_path(path):
    if not isinstance(path,str) or not path.startswith(ROOT+'/') or any(p in ('','..','.') for p in path[len(ROOT)+1:].split('/')):
        raise DiskError('Недопустимый путь в папке приложения.')
    return path

class Client:
    def __init__(self,token,opener=None):
        if not isinstance(token,str) or not 20<=len(token)<=8192 or any(ord(c)<33 or ord(c)>126 for c in token):
            raise DiskError('Проверьте OAuth-токен.')
        self.token=token
        self.opener=opener or build_opener(HTTPSHandler(context=ssl.create_default_context()),Redirects())

    @sensitive_variables()
    def request(self,method='GET',path='',params=None):
        url=API+path+('?' + urlencode(params) if params else '')
        req=Request(url,method=method,headers={'Authorization':'OAuth '+self.token,'Accept':'application/json'})
        try:
            with self.opener.open(req,timeout=12) as response:
                raw=response.read(1024*1024+1)
                if len(raw)>1024*1024:raise DiskError('Ответ Диска слишком большой.')
                data=json.loads(raw) if raw else {}
                if not isinstance(data,dict):raise DiskError('Некорректный ответ API Диска.')
                return data
        except HTTPError as exc:
            if exc.code==404:raise FileNotFoundError('Объект не найден на Диске.') from None
            if exc.code==409:raise FileExistsError('Объект уже есть на Диске.') from None
            raise DiskError('Яндекс Диск отклонил запрос (HTTP '+str(exc.code)+'). Проверьте токен, разрешения и свободное место.') from None
        except (URLError,OSError,ValueError):raise DiskError('Не удалось получить ответ Яндекс Диска.') from None

    def info(self):
        data=self.request(params={'fields':'total_space,used_space'})
        root=self.request('GET','/resources',{'path':'app:/','fields':'type'})
        if root.get('type')!='dir':raise DiskError('Нет доступа к папке приложения.')
        try:total=int(data['total_space']);used=int(data['used_space'])
        except (KeyError,ValueError,TypeError):raise DiskError('Не удалось проверить свободное место.') from None
        if total<0 or used<0:raise DiskError('Некорректная квота Диска.')
        return total,used

    def metadata(self,path):
        return self.request('GET','/resources',params={'path':app_path(path),'fields':'type,size,sha256'})

    def mkdirs(self):
        for path in (ROOT,ROOT+'/backups'):
            try:self.request('PUT','/resources',{'path':path})
            except FileExistsError:
                meta=self.request('GET','/resources',{'path':path,'fields':'type'})
                if meta.get('type')!='dir':raise DiskError('Путь резервных копий занят файлом.')

    def upload(self,path,source):
        path=app_path(path)
        data=self.request('GET','/resources/upload',{'path':path,'overwrite':'false'})
        href=signed_url(data.get('href'))
        if data.get('method','PUT')!='PUT':raise DiskError('Некорректный способ загрузки.')
        try:
            with open(source,'rb') as stream:
                req=Request(href,data=stream,method='PUT',headers={'Content-Length':str(Path(source).stat().st_size),'Content-Type':'application/octet-stream'})
                with self.opener.open(req,timeout=120) as response:
                    if response.status not in (200,201,202):raise DiskError('Загрузка копии не подтверждена.')
        except (HTTPError,URLError,OSError):raise DiskError('Загрузка прервана. Локальная резервная копия сохранена.') from None

    def verify(self,path,expected_hash,expected_size):
        link=self.request('GET','/resources/download',{'path':app_path(path)})
        href=signed_url(link.get('href'))
        if link.get('method','GET')!='GET':raise DiskError('Некорректный способ скачивания.')
        sha=hashlib.sha256();size=0
        try:
            with self.opener.open(Request(href,method='GET'),timeout=120) as response:
                while True:
                    block=response.read(1024*1024)
                    if not block:break
                    size+=len(block)
                    if size>expected_size:raise DiskError('Размер скачанной копии не совпал.')
                    sha.update(block)
        except (HTTPError,URLError,OSError):raise DiskError('Не удалось проверить скачивание копии.') from None
        if size!=expected_size or sha.hexdigest()!=expected_hash:raise DiskError('Контрольная сумма скачанной копии не совпала.')
