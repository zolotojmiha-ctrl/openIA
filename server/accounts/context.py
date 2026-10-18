from django.conf import settings

def local_testing(request):
    return {'lan_test': settings.LAN_TEST}
