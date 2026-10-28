from urllib.parse import urlsplit
from django.urls import resolve,Resolver404,reverse

def return_url(request,fallback):
    value=request.POST.get('return_to') or request.GET.get('return_to','')
    if len(value)>4096:return fallback
    try:parsed=urlsplit(value)
    except ValueError:return fallback
    if parsed.scheme or parsed.netloc or not value.startswith('/cabinet/') or '\\' in value:return fallback
    try:match=resolve(parsed.path)
    except Resolver404:return fallback
    return value if match.url_name in ('repairs','work_place','repair_detail') else fallback

def detail_url(request,pk):
    from urllib.parse import urlencode
    base=reverse('repair_detail',args=[pk])
    back=return_url(request,'')
    return base+('?' + urlencode({'return_to':back}) if back else '')
