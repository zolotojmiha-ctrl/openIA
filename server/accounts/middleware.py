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
        response=self.get_response(request)
        if request.path.startswith('/cabinet/'):
            response['Cache-Control']='no-store, private'
            response['Pragma']='no-cache'
            response['X-Robots-Tag']='noindex, nofollow'
        return response
