from django.urls import path
from accounts import views
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
]
