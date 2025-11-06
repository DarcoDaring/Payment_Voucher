# vouchers/templatetags/voucher_tags.py
from django import template

register = template.Library()


@register.filter
def first_where(lst, condition_str):
    """
    Usage in template:
        {% with next_level=approval_levels|first_where:"is_next=True" %}
    Returns the first item in list where condition is True.
    condition_str format: "key=value" or "key=True"
    """
    if not lst or not condition_str:
        return None

    try:
        key, value = condition_str.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"\'')
        is_bool = value.lower() in ('true', 'false')
        target = True if value.lower() == 'true' else False if value.lower() == 'false' else value
    except:
        return None

    for item in lst:
        val = getattr(item, key, None)
        if val is not None:
            if is_bool:
                if bool(val) == target:
                    return item
            else:
                if str(val) == str(target):
                    return item
    return None


@register.filter
def sum_particulars(queryset):
    """Sum all amounts in particulars"""
    from decimal import Decimal
    total = sum(p.amount for p in queryset if p.amount)
    return total or Decimal('0.00')


@register.filter
def sub(a, b):
    """Subtract b from a"""
    return a - b