# vouchers/templatetags/voucher_extras.py
from django import template
import os

register = template.Library()

@register.filter
def sum_amount(queryset):
    return sum(p.amount for p in queryset)


@register.filter
def filename(value):
    """
    Returns the base filename from a file path or URL.
    Usage: {{ file_field.name|filename }} → "invoice.pdf"
    Safely handles None and empty values.
    """
    if not value:
        return ""
    return os.path.basename(str(value))


@register.filter
def split(value, arg):
    """
    Splits a string by delimiter.
    Usage: {{ "hello.world.jpg"|split:"."|last }} → "jpg"
    """
    if not value:
        return []
    return str(value).split(arg)


@register.filter
def first_rejected(approvals):
    """
    Returns the first REJECTED approval from a queryset, or None.
    """
    return approvals.filter(status='REJECTED').first()