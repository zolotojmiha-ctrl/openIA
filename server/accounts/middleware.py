from django.contrib.auth import logout
from django.shortcuts import redirect

class AccountStateMiddleware:
    def __init__(self,get_response):
        self.get_response=get_response
    def __call__(self,request):
        if request.user.is_authenticated:
            if not request.user.is_active:
                logout(request)
                return redirect('login')
            if request.user.must_change_password and request.path.startswith('/cabinet/') and request.path not in ['/cabinet/password/','/cabinet/logout/']:
                return redirect('password')
        if request.user.is_authenticated and request.user.role=='lessor' and not request.user.is_leader and request.path.startswith('/cabinet/'):
            from django.urls import resolve,Resolver404
            from django.http import HttpResponseForbidden
            try:name=resolve(request.path).url_name
            except Resolver404:name=None
            allowed={'dashboard','equipment','lessor_home','lessor_detail','lessor_export','profile','password','logout','end_sessions','max_connection','max_onboarding'}
            if name not in allowed:
                response=HttpResponseForbidden('Этот раздел недоступен арендодателю.')
                response['Cache-Control']='private, no-store'
                return response
        if request.user.is_authenticated and request.user.role=='customer' and not request.user.is_leader:
            from django.http import HttpResponseForbidden
            allowed=('/cabinet/customer/','/cabinet/password/','/cabinet/logout/')
            if request.path=='/cabinet/':return redirect('customer_portal')
            if request.path.startswith('/cabinet/') and not any(request.path.startswith(p) for p in allowed):
                return HttpResponseForbidden('Этот раздел недоступен заказчику.')
        if request.user.is_authenticated and request.session.session_key:
            from .models import UserSession
            from django.utils import timezone
            now=int(timezone.now().timestamp())
            if now-request.session.get('_session_seen',0)>=60:
                UserSession.objects.update_or_create(session_key=request.session.session_key,defaults={'user':request.user,'device':request.headers.get('User-Agent','')[:200]})
                request.session['_session_seen']=now
        response=self.get_response(request)
        if request.path.startswith('/cabinet/'):
            response['Cache-Control']='no-store, private'
            response['Pragma']='no-cache'
            response['X-Robots-Tag']='noindex, nofollow'
        return response


class ContentPolicyMiddleware:
    """Incremental CSP; private previews retain their stronger per-response policy."""
    def __init__(self,get_response):self.get_response=get_response
    def __call__(self,request):
        response=self.get_response(request)
        response.setdefault('Content-Security-Policy',"object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        return response
