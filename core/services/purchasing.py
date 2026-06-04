"""Purchasing helpers: supplier price-history capture and lookup, plus
return-to-supplier credit notes."""
from decimal import Decimal

from django.db.models import Avg
from django.db import transaction
from django.utils import timezone

from core.models import SupplierPriceHistory


def record_supplier_price(*, tenant, supplier, product, unit_cost, source, reference=None,
                          currency_code="GBP", recorded_at=None):
    """Record a supplier+product unit cost. Idempotent per (supplier, product,
    source, reference) so re-submitting a PO or re-posting a bill won't duplicate.
    No-op when the supplier/product/cost is missing or non-positive."""
    if supplier is None or product is None or unit_cost is None:
        return None
    unit_cost = Decimal(unit_cost)
    if unit_cost <= Decimal("0.00"):
        return None
    defaults = {"unit_cost": unit_cost, "currency_code": currency_code or "GBP"}
    if recorded_at is not None:
        defaults["recorded_at"] = recorded_at
    obj, _ = SupplierPriceHistory.objects.update_or_create(
        tenant=tenant, supplier=supplier, product=product,
        source=source, reference=(reference or ""), defaults=defaults,
    )
    return obj


def record_po_prices(po):
    """Capture the agreed price for every line of a submitted PO."""
    for line in po.lines.select_related("product"):
        record_supplier_price(
            tenant=po.tenant, supplier=po.supplier, product=line.product,
            unit_cost=line.unit_cost, source=SupplierPriceHistory.Source.PO,
            reference=po.po_number, currency_code=getattr(po, "currency_code", "GBP"),
            recorded_at=po.created_at.date(),
        )


def record_bill_prices(inv):
    """Capture the actual billed price for every line of a posted supplier bill."""
    for line in inv.lines.select_related("product"):
        if line.product_id is None:
            continue
        record_supplier_price(
            tenant=inv.tenant, supplier=inv.supplier, product=line.product,
            unit_cost=line.unit_cost, source=SupplierPriceHistory.Source.BILL,
            reference=inv.invoice_number, currency_code=getattr(inv, "currency_code", "GBP"),
            recorded_at=inv.invoice_date,
        )


def last_prices_for_supplier(tenant, supplier):
    """Return {product_id: latest_unit_cost} for a supplier (most recent record wins)."""
    out = {}
    for rec in (SupplierPriceHistory.objects
                .filter(tenant=tenant, supplier=supplier)
                .order_by("product_id", "-recorded_at", "-id")):
        out.setdefault(rec.product_id, rec.unit_cost)
    return out


def average_price(tenant, supplier, product):
    agg = (SupplierPriceHistory.objects
           .filter(tenant=tenant, supplier=supplier, product=product)
           .aggregate(a=Avg("unit_cost")))
    return agg["a"]


def supplier_scorecard(tenant, date_from, date_to):
    """Per-supplier performance for the period: spend, on-time delivery and
    purchase price variance.

    - spend: total of posted supplier bills (by invoice date).
    - OTD: posted GRNs received on/before the PO's expected date, as a % of GRNs
      that had an expected date.
    - price variance: sum over matched bill lines of (billed - ordered) unit cost
      x qty (positive = paid more than the PO price)."""
    from core.models import SupplierInvoice, GoodsReceipt
    Z = Decimal("0.00")
    rows = {}

    def row(supplier):
        return rows.setdefault(supplier.id, {
            "supplier": supplier, "spend": Z, "bills": 0,
            "receipts": 0, "on_time": 0, "rated": 0, "price_variance": Z,
        })

    bills = (SupplierInvoice.objects
             .filter(tenant=tenant, status="POSTED",
                     invoice_date__gte=date_from, invoice_date__lte=date_to)
             .select_related("supplier")
             .prefetch_related("lines", "lines__po_line", "lines__tax_code"))
    for b in bills:
        r = row(b.supplier)
        r["spend"] += b.total
        r["bills"] += 1
        for l in b.lines.all():
            if l.po_line_id:
                r["price_variance"] += (l.unit_cost - l.po_line.unit_cost) * (l.qty or Z)

    grns = (GoodsReceipt.objects
            .filter(tenant=tenant, status="POSTED",
                    received_at__date__gte=date_from, received_at__date__lte=date_to)
            .select_related("po", "po__supplier"))
    for g in grns:
        if g.po.supplier_id is None:
            continue
        r = row(g.po.supplier)
        r["receipts"] += 1
        if g.po.expected_date:
            r["rated"] += 1
            if g.received_at.date() <= g.po.expected_date:
                r["on_time"] += 1

    out = []
    for r in rows.values():
        r["otd_pct"] = ((Decimal(r["on_time"]) / r["rated"] * Decimal("100")).quantize(Decimal("0.1"))
                        if r["rated"] else None)
        out.append(r)
    out.sort(key=lambda r: r["spend"], reverse=True)
    return {"rows": out, "total_spend": sum((r["spend"] for r in out), Z)}


@transaction.atomic
def create_return_credit_note(adj, value, user=None):
    """Create and post a purchase credit note for a return-to-supplier stock
    adjustment, adjusting Accounts Payable. `value` is the (negative) costed
    movement value; the credit is raised for its absolute amount against the
    Inventory account (DR AP / CR Inventory). Returns the credit note, or None
    when there's no supplier or no value."""
    from core.models import CreditNote, CreditNoteLine
    from core.numbering import next_document_number
    from core.services.gl import post_credit_note

    if adj.supplier_id is None:
        return None
    amount = abs(Decimal(value or "0.00"))
    if amount <= Decimal("0.00"):
        return None

    cn = CreditNote.objects.create(
        tenant=adj.tenant,
        kind=CreditNote.Kind.PURCHASE,
        credit_note_number=next_document_number(CreditNote, adj.tenant, "credit_note_number", "PCN-"),
        credit_note_date=timezone.localdate(),
        supplier=adj.supplier,
        reason=f"Return to supplier: {adj.product.sku} x{abs(adj.qty_delta)}"
               + (f" ({adj.notes})" if adj.notes else ""),
        currency_code=getattr(adj.tenant, "currency_code", "GBP") or "GBP",
    )
    CreditNoteLine.objects.create(
        credit_note=cn,
        product=adj.product,
        description=f"Returned to {adj.supplier.name}",
        qty=abs(adj.qty_delta),
        unit_amount=(amount / abs(adj.qty_delta)) if adj.qty_delta else amount,
        tax_code=None,            # net-only: keeps inventory + AP balanced
        account=None,             # defaults to Inventory for purchase credits
    )
    post_credit_note(cn, user=user)
    return cn
