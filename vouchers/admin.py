from django.contrib import admin
from .models import Voucher, Particular

@admin.register(Voucher)
class VoucherAdmin(admin.ModelAdmin):
    list_display = ['voucher_number', 'pay_to', 'status', 'created_by', 'created_at']
    list_filter = ['status', 'payment_type', 'created_by']
    search_fields = ['voucher_number', 'pay_to']
    readonly_fields = ['voucher_number', 'created_by', 'created_at']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_staff:
            return qs
        return qs.filter(created_by=request.user)

@admin.register(Particular)
class ParticularAdmin(admin.ModelAdmin):
    list_display = ['voucher', 'description', 'amount']
    list_filter = ['voucher']
    search_fields = ['description']