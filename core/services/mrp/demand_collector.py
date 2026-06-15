"""Demand collection for MRP (Phase 2: sales orders + safety stock).

Per item-site planning profile, records:
- SALES_ORDER demand from open (CONFIRMED) customer-order lines. Customer
  orders have no required/ship date, so the required date is inferred from the
  order date (then the run's planning start), and a warning is raised.
- one SAFETY_STOCK demand row representing the target floor (informational /
  peg target; the engine treats safety stock as a floor, not gross demand, so
  this is not double-counted).

No sales / order schema is changed. Quantities convert to the product base unit
via the existing UOM service; a missing conversion is reported, not fatal.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError

from core.services.mrp import exceptions as mrp_exc

ZERO = Decimal("0.00")


def _to_base(product, qty, uom, run):
    from core.services.uom import to_base_qty
    try:
        return to_base_qty(product, qty, uom)
    except ValidationError:
        mrp_exc.raise_exception(
            run, "MISSING_UOM_CONVERSION",
            f"No UOM conversion for {product.sku}; quantity used as entered.",
            product=product, dedupe_key=("uom", product.id))
        return Decimal(qty or 0)


def collect(run, profile):
    """Create and return the list of MRPDemand rows for one planning profile."""
    from core.models import CustomerOrderLine, MRPDemand

    tenant = run.tenant
    product = profile.product
    site = profile.site
    start = run.planning_start_date
    created = []

    if run.include_sales_orders and profile.include_sales_orders:
        lines = (CustomerOrderLine.objects
                 .filter(order__tenant=tenant, order__status="CONFIRMED",
                         product=product, order__site=site)
                 .select_related("order", "product", "uom"))
        for line in lines:
            order = line.order
            base_qty = _to_base(product, line.qty, line.uom, run)
            if base_qty <= ZERO:
                continue

            required_date = order.order_date or start
            # Customer orders never carry a required/ship date, so this date is
            # always inferred - tell the planner (once per order).
            mrp_exc.raise_exception(
                run, "SALES_ORDER_REQUIRED_DATE_MISSING",
                f"Sales order {order.order_number} has no required date; "
                f"used order date {required_date}.",
                product=product, site=site,
                source_document_type="CustomerOrder", source_document_id=order.order_number,
                dedupe_key=("so_date", order.id))

            if required_date < start:
                mrp_exc.raise_exception(
                    run, "PAST_DUE_DEMAND",
                    f"Sales order {order.order_number} demand for {product.sku} is past due "
                    f"({required_date}).",
                    product=product, site=site,
                    source_document_type="CustomerOrder", source_document_id=order.order_number,
                    dedupe_key=("so_pastdue", line.id))

            created.append(MRPDemand.objects.create(
                mrp_run=run, tenant=tenant, product=product, site=site,
                demand_type="SALES_ORDER",
                source_document_type="CustomerOrder", source_document_id=order.order_number,
                source_line_id=str(line.id),
                required_date=required_date, quantity=base_qty, open_quantity=base_qty,
                priority=0,
            ))

    # Safety stock target (floor) as an informational / peggable demand row.
    if run.include_safety_stock and profile.include_safety_stock and (profile.safety_stock_qty or ZERO) > ZERO:
        created.append(MRPDemand.objects.create(
            mrp_run=run, tenant=tenant, product=product, site=site,
            demand_type="SAFETY_STOCK",
            source_document_type="ItemSitePlanning", source_document_id=str(profile.id),
            required_date=start, quantity=profile.safety_stock_qty, open_quantity=profile.safety_stock_qty,
            priority=0,
        ))

    return created
