# vouchers/templatetags/voucher_tags.py
from django import template
from django.db.models import Sum

register = template.Library()


@register.filter
def sum_particulars(queryset):
    """Sum all amounts in a particular queryset."""
    return queryset.aggregate(total=Sum('amount'))['total'] or 0


@register.filter
def sub(a, b):
    """Subtract b from a (safe for template use)."""
    try:
        return int(a) - int(b)
    except Exception:
        return 0


@register.filter
def attachment_count(queryset):
    """Count how many particulars have an attachment."""
    return queryset.filter(attachment__isnull=False).count()