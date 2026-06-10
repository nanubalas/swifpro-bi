"""Operational worklist: stock stuck in transit or stuck in hold/quarantine.

Read-only/advisory — it surfaces items that need a human to act (receive/cancel a
stale transfer, release/scrap a held return) and links to the existing action
pages. It changes no inventory, costing or GL.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from core.models import InventoryTransfer, ReturnLine, ReturnAuthorization

ZERO = Decimal("0.00")
DEFAULT_AGE_DAYS = 7


def _high_value_cutoff(tenant):
    """Reuse the stock-adjustment approval threshold as the 'high value' bar
    (0 = no threshold, so high-value filtering shows everything)."""
    return tenant.stock_adjustment_approval_threshold or ZERO


def stale_transfers(tenant, *, days=DEFAULT_AGE_DAYS, location_id=None, product_id=None,
                    high_value_only=False, now=None):
    """Two-step transfers still in transit (DISPATCHED with quantity not yet
    received) older than `days`. Partially-received transfers stay DISPATCHED, so
    they're included while any in-transit qty remains."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=days)
    hv = _high_value_cutoff(tenant) if high_value_only else None

    qs = (InventoryTransfer.objects
          .filter(tenant=tenant, status=InventoryTransfer.Status.DISPATCHED)
          .select_related("from_location", "to_location")
          .prefetch_related("lines__product"))
    if location_id:
        from django.db.models import Q
        qs = qs.filter(Q(from_location_id=location_id) | Q(to_location_id=location_id))

    out = []
    for tr in qs:
        dispatched = tr.dispatched_at or tr.created_at
        if dispatched > cutoff:
            continue
        lines = list(tr.lines.all())
        if product_id and not any(l.product_id == product_id for l in lines):
            continue
        in_transit_qty = sum((l.in_transit_qty for l in lines), ZERO)
        if in_transit_qty <= ZERO:
            continue
        value = sum(((l.in_transit_qty or ZERO) * (l.dispatched_unit_cost or ZERO)) for l in lines)
        value = value.quantize(Decimal("0.01")) if value else ZERO
        if hv is not None and hv > ZERO and value < hv:
            continue
        moving = [l for l in lines if (l.in_transit_qty or ZERO) > ZERO]
        out.append({
            "id": tr.id, "transfer_number": tr.transfer_number, "status": tr.status,
            "from_location": tr.from_location.name, "to_location": tr.to_location.name,
            "dispatched_at": dispatched, "age_days": (now - dispatched).days,
            "in_transit_qty": in_transit_qty, "value": value,
            "line_count": len(moving),
            "summary": ", ".join(f"{l.product.sku} ×{l.in_transit_qty}" for l in moving[:4])
                       + (" …" if len(moving) > 4 else ""),
            "url": f"/transfers/{tr.id}/",
        })
    out.sort(key=lambda r: r["age_days"], reverse=True)
    return out


def unresolved_holds(tenant, *, days=DEFAULT_AGE_DAYS, location_id=None, disposition=None,
                     product_id=None, high_value_only=False, now=None):
    """Received RMA lines on a non-sellable hold (QUARANTINE / REPAIR /
    RETURN_TO_SUPPLIER) that haven't been released or scrapped, older than
    `days`."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=days)
    hv = _high_value_cutoff(tenant) if high_value_only else None

    qs = (ReturnLine.objects
          .filter(rma__tenant=tenant, rma__status=ReturnAuthorization.Status.RECEIVED,
                  disposition__in=list(ReturnLine.HOLD_DISPOSITIONS), final_disposition__isnull=True)
          .select_related("rma", "rma__receive_location", "product"))
    if disposition:
        qs = qs.filter(disposition=disposition)
    if product_id:
        qs = qs.filter(product_id=product_id)
    if location_id:
        qs = qs.filter(rma__receive_location_id=location_id)

    out = []
    for l in qs:
        received = l.inspected_at or l.rma.created_at
        if received > cutoff:
            continue
        qty = l.qty or ZERO
        value = (qty * (l.product.cost_price or ZERO)).quantize(Decimal("0.01"))
        if hv is not None and hv > ZERO and value < hv:
            continue
        out.append({
            "rma_id": l.rma_id, "rma_number": l.rma.rma_number, "channel": l.rma.channel,
            "reference": l.rma.original_order_number or "",
            "product": l.product.name, "sku": l.product.sku,
            "serial_number": l.serial_number or "", "lot_code": l.lot_code or "",
            "location": getattr(l.rma.receive_location, "name", ""),
            "disposition": l.disposition, "disposition_label": l.get_disposition_display(),
            "received_at": received, "age_days": (now - received).days,
            "qty": qty, "value": value,
            "url": f"/returns/{l.rma_id}/",
        })
    out.sort(key=lambda r: r["age_days"], reverse=True)
    return out


def worklist_counts(tenant, *, days=DEFAULT_AGE_DAYS):
    """Counts for dashboard surfacing."""
    return {
        "stale_transfers": len(stale_transfers(tenant, days=days)),
        "unresolved_holds": len(unresolved_holds(tenant, days=days)),
    }
