"""Pegging: link planned orders (supply) to the demand they cover.

Allocation is earliest-required-date first, with real sales demand taking
priority over the safety-stock floor on the same date. Returns the demand rows
left uncovered so the engine can raise SHORTAGE exceptions.
"""
from decimal import Decimal

ZERO = Decimal("0.00")

# Lower sorts first: sales demand pegged before the safety-stock floor.
_DEMAND_RANK = {"SALES_ORDER": 0, "TRANSFER_REQUEST": 0, "WORK_ORDER_COMPONENT": 0,
                "FORECAST": 1, "SERVICE": 1, "SAFETY_STOCK": 2}


def peg(run, planned_orders, demands):
    """Create MRPPegging rows. Returns ``[(demand, uncovered_qty), ...]``."""
    from core.models import MRPPegging

    queue = sorted(demands, key=lambda d: (d.required_date, _DEMAND_RANK.get(d.demand_type, 1)))
    for d in queue:
        d._remaining = d.open_quantity or ZERO

    for po in sorted(planned_orders, key=lambda p: p.required_date):
        po_remaining = po.quantity or ZERO
        if po_remaining <= ZERO:
            continue
        for d in queue:
            if po_remaining <= ZERO:
                break
            if d._remaining <= ZERO:
                continue
            alloc = min(po_remaining, d._remaining)
            MRPPegging.objects.create(
                tenant=run.tenant, planned_order=po, demand=d,
                pegged_quantity=alloc, required_date=d.required_date,
                supply_date=po.planned_receipt_date, shortage_quantity=ZERO,
            )
            po_remaining -= alloc
            d._remaining -= alloc

    return [(d, d._remaining) for d in queue if d._remaining > ZERO]
