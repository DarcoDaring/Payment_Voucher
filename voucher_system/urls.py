from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.auth import views as auth_views
from vouchers.views import (
    HomeView, VoucherListView, VoucherDetailView,
    VoucherCreateAPI, VoucherApprovalAPI,
    DesignationCreateAPI, ApprovalControlAPI,
    UserCreateAPI, VoucherDeleteAPI
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', HomeView.as_view(), name='home'),
    path('vouchers/', VoucherListView.as_view(), name='voucher_list'),
    path('vouchers/<int:pk>/', VoucherDetailView.as_view(), name='voucher_detail'),
    path('api/vouchers/create/', VoucherCreateAPI.as_view(), name='voucher_create_api'),
    path('api/vouchers/<int:pk>/approve/', VoucherApprovalAPI.as_view(), name='voucher_approval_api'),
    path('api/designations/create/', DesignationCreateAPI.as_view(), name='designation_create_api'),
    path('api/approval/control/', ApprovalControlAPI.as_view(), name='approval_control_api'),
    path('api/users/create/', UserCreateAPI.as_view(), name='user_create_api'),
    path('accounts/login/', auth_views.LoginView.as_view(template_name='login.html'), name='login'),
    path('accounts/', include('django.contrib.auth.urls')),
    path('api/vouchers/<int:pk>/delete/', VoucherDeleteAPI.as_view(), name='voucher_delete_api'),
]

# Serve media files in both local and production
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)