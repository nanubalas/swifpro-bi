"""Purchasing helpers: supplier price-history capture and lookup."""
from decimal import Decimal

from django.db.models import Avg

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
