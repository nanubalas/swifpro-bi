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


def _pct(margin, revenue):
    if revenue and revenue != ZERO:
        return (margin / revenue * Decimal("100")).quantize(Decimal("0.1"))
    return ZERO


def profitability(tenant, date_from, date_to):
    """Gross margin (revenue - COGS) by product and by customer for the period.

    Revenue is the net (ex-VAT) of issued invoice lines. COGS is the costed value
    of the SALE inventory movements posted for those invoices, so margin reflects
    moving-average / FIFO / standard cost consistently with the GL. Service or
    description-only lines carry no COGS (100% margin)."""
    from core.models import InventoryMovement
    invoices = list(_invoices(tenant, date_from, date_to))
    inv_by_number = {i.invoice_number: i for i in invoices}

    # COGS from SALE movements tied to these invoices.
    cogs_by_product, cogs_by_customer = {}, {}
    movements = (InventoryMovement.objects
                 .filter(tenant=tenant, movement_type="SALE", ref_type="AR_INVOICE",
                         ref_id__in=list(inv_by_number.keys()))
                 .select_related("product"))
    for m in movements:
        cost = -(m.value or ZERO)
        cogs_by_product[m.product_id] = cogs_by_product.get(m.product_id, ZERO) + cost
        inv = inv_by_number.get(m.ref_id)
        if inv is not None:
            cogs_by_customer[inv.customer_id] = cogs_by_customer.get(inv.customer_id, ZERO) + cost

    # Revenue by product + customer.
    prod, cust = {}, {}
    for inv in invoices:
        cb = cust.setdefault(inv.customer_id, {"name": inv.customer.name, "revenue": ZERO})
        for l in inv.lines.all():
            cb["revenue"] += l.line_total
            if l.product_id:
                pb = prod.setdefault(l.product_id, {"key": l.product.sku, "name": l.product.name, "revenue": ZERO})
                pb["revenue"] += l.line_total

    def finish(buckets, cogs_map):
        rows = []
        for pid, b in buckets.items():
            cogs = cogs_map.get(pid, ZERO)
            margin = b["revenue"] - cogs
            rows.append({**b, "cogs": cogs, "margin": margin, "margin_pct": _pct(margin, b["revenue"])})
        rows.sort(key=lambda r: r["margin"], reverse=True)
        return rows

    by_product = finish(prod, cogs_by_product)
    by_customer = finish(cust, cogs_by_customer)
    totals = {
        "revenue": sum((r["revenue"] for r in by_customer), ZERO),
        "cogs": sum((r["cogs"] for r in by_customer), ZERO),
    }
    totals["margin"] = totals["revenue"] - totals["cogs"]
    totals["margin_pct"] = _pct(totals["margin"], totals["revenue"])
    return {"by_product": by_product, "by_customer": by_customer, "totals": totals}


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
