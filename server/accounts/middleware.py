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
