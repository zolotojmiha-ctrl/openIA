from django.urls import path
from accounts import views
from projects import views as projects
urlpatterns=[
 path('',views.public_site,name='public'),
 path('portal/',views.prototype,name='prototype'),
 path('portal/<str:filename>',views.prototype),
 path('cabinet/',views.dashboard,name='dashboard'),
 path('cabinet/login/',views.login_view,name='login'),
 path('cabinet/logout/',views.logout_view,name='logout'),
 path('cabinet/password/',views.password_view,name='password'),
 path('cabinet/users/',views.users,name='users'),
 path('cabinet/users/new/',views.employee_edit,name='employee_new'),
 path('cabinet/users/<uuid:pk>/',views.employee_edit,name='employee_edit'),
 path('cabinet/users/<uuid:pk>/reset/',views.reset_password,name='employee_reset'),
 path('cabinet/audit/',views.audit_view,name='audit'),
 path('cabinet/objects/',projects.objects,name='objects'),
 path('cabinet/objects/new/',projects.object_edit,name='object_new'),
 path('cabinet/objects/<uuid:pk>/',projects.object_detail,name='object_detail'),
 path('cabinet/objects/<uuid:pk>/edit/',projects.object_edit,name='object_edit'),
 path('cabinet/objects/<uuid:pk>/access/',projects.assignments,name='object_assignments'),
 path('cabinet/contracts/',projects.contracts,name='contracts'),
 path('cabinet/contracts/new/',projects.contract_edit,name='contract_new'),
 path('cabinet/contracts/<uuid:pk>/',projects.contract_detail,name='contract_detail'),
 path('cabinet/contracts/<uuid:pk>/edit/',projects.contract_edit,name='contract_edit'),
 path('cabinet/contracts/<uuid:pk>/access/',projects.share_contract,name='contract_share'),
]
