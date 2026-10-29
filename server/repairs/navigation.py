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

def report_return_url(request,job):
    fallback=reverse('work_place',args=[job.contract_id,job.section.project_object_id]) if job.contract_id else reverse('repairs')
    back=return_url(request,fallback)
    match=resolve(urlsplit(back).path)
    if match.url_name=='repair_detail':
        from urllib.parse import parse_qs
        nested=parse_qs(urlsplit(back).query).get('return_to',[''])[0]
        if nested and len(nested)<=4096 and '\\' not in nested:
            try:
                parsed=urlsplit(nested)
                if not parsed.scheme and not parsed.netloc and resolve(parsed.path).url_name in ('repairs','work_place'):return nested
            except (ValueError,Resolver404):pass
        return fallback
    return back
