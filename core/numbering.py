"""Auto-generated, sequential document numbers (invoices, quotes, orders).

Numbers look like ``INV-0001``. They are generated per tenant by finding the
highest existing numeric suffix for the prefix and adding one, but every form
keeps the field editable so an admin can override the suggested number.
"""
import re


def next_document_number(model, tenant, field, prefix, pad=4):
    """Return the next ``<prefix><n>`` for a tenant, scanning existing values
    of ``field`` on ``model``."""
    existing = model.objects.filter(tenant=tenant, **{f"{field}__startswith": prefix}) \
        .values_list(field, flat=True)
    highest = 0
    pat = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    for value in existing:
        m = pat.match(value or "")
        if m:
            highest = max(highest, int(m.group(1)))
    return f"{prefix}{highest + 1:0{pad}d}"


def next_invoice_number(tenant):
    from core.models import CustomerInvoice
    return next_document_number(CustomerInvoice, tenant, "invoice_number", "INV-")
