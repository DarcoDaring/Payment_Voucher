# vouchers/templatetags/voucher_extras.py
from django import template

register = template.Library()

@register.filter
def sum_amount(queryset):
    return sum(p.amount for p in queryset)