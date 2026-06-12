"""Template tag for the reusable field-help info icon + popover.

Usage in a form template:

    {% load field_help_tags %}
    <label class="form-label">SKU {% field_help form.sku %}</label>{{ form.sku }}

Renders a small info icon; clicking it shows business help for everyone and,
for permitted users, a collapsible "Technical details" section. Works on any
bound field via fallback metadata.
"""
from django import template

from core import field_help as fh

register = template.Library()


@register.inclusion_tag("field_help/_widget.html", takes_context=True)
def field_help(context, field, label=None):
    can_tech = fh.can_view_technical(context)
    biz = fh.business_help(field)
    tech = fh.technical_metadata(field) if can_tech else None
    auto_id = getattr(field, "auto_id", "") or getattr(field, "html_name", "") or "field"
    return {
        "biz": biz,
        "tech": tech,
        "can_tech": can_tech,
        "label": label or biz["label"],
        "pop_id": "fh-" + str(auto_id),
    }
