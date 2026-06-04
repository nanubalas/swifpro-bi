"""Sales analytics: history and breakdowns by product, customer and channel.

Built from issued customer invoices (status Issued/Sent/Paid) in the period.
"""
from decimal import Decimal

from core.models import CustomerInvoice, SalesOrder

ZERO = Decimal("0.00")
SALE_STATES = ("ISSUED", "SENT", "PAID")


def _invoices(tenant, date_from, date_to):
    return (CustomerInvoice.objects
            .filter(tenant=tenant, status__in=SALE_STATES,
                    invoice_date__gte=date_from, invoice_date__lte=date_to)
            .select_related("customer")
            .prefetch_related("lines", "lines__tax_code", "lines__product"))


def sales_history(tenant, date_from, date_to):
    rows, net_total, vat_total, grand = [], ZERO, ZERO, ZERO
    for inv in _invoices(tenant, date_from, date_to).order_by("invoice_date", "id"):
        rows.append({"invoice": inv, "net": inv.subtotal, "vat": inv.tax_total, "total": inv.total})
        net_total += inv.subtotal
        vat_total += inv.tax_total
        grand += inv.total
    return {"rows": rows, "net_total": net_total, "vat_total": vat_total, "grand_total": grand}


def sales_by_product(tenant, date_from, date_to):
    buckets = {}
    for inv in _invoices(tenant, date_from, date_to):
        for l in inv.lines.all():
            key = l.product.sku if l.product_id else (l.description or "(unspecified)")
            name = l.product.name if l.product_id else (l.description or "(unspecified)")
            b = buckets.setdefault(key, {"key": key, "name": name, "qty": ZERO, "net": ZERO, "total": ZERO})
            b["qty"] += (l.qty or ZERO)
            b["net"] += l.line_total
            b["total"] += l.line_total + l.tax_amount
    rows = sorted(buckets.values(), key=lambda r: r["total"], reverse=True)
    return {"rows": rows, "net_total": sum((r["net"] for r in rows), ZERO),
            "grand_total": sum((r["total"] for r in rows), ZERO)}


def sales_by_customer(tenant, date_from, date_to):
    buckets = {}
    for inv in _invoices(tenant, date_from, date_to):
        b = buckets.setdefault(inv.customer_id, {"name": inv.customer.name, "count": 0, "net": ZERO, "total": ZERO})
        b["count"] += 1
        b["net"] += inv.subtotal
        b["total"] += inv.total
    rows = sorted(buckets.values(), key=lambda r: r["total"], reverse=True)
    return {"rows": rows, "net_total": sum((r["net"] for r in rows), ZERO),
            "grand_total": sum((r["total"] for r in rows), ZERO)}


def sales_by_channel(tenant, date_from, date_to):
    """Direct customer invoices plus posted channel (ecommerce) orders, grouped
    by channel."""
    rows = []
    # Direct sales = customer invoices.
    inv_qs = _invoices(tenant, date_from, date_to)
    direct_total = sum((inv.total for inv in inv_qs), ZERO)
    direct_count = len(inv_qs)
    if direct_count:
        rows.append({"channel": "Direct (invoices)", "count": direct_count, "total": direct_total})

    # Channel/ecommerce orders (posted) grouped by their sales channel.
    by_channel = {}
    orders = (SalesOrder.objects
              .filter(tenant=tenant, status="POSTED",
                      order_date__date__gte=date_from, order_date__date__lte=date_to)
              .prefetch_related("lines"))
    for o in orders:
        b = by_channel.setdefault(o.channel, {"channel": o.get_channel_display(), "count": 0, "total": ZERO})
        b["count"] += 1
        b["total"] += sum((l.line_total for l in o.lines.all()), ZERO)
    rows.extend(sorted(by_channel.values(), key=lambda r: r["total"], reverse=True))
    return {"rows": rows, "grand_total": sum((r["total"] for r in rows), ZERO)}
