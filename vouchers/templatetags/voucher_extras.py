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
    Usage: {{ file_field.name|filename }} → "image.jpg"
    """
    return os.path.basename(str(value))