"""Forecast consumption (Phase 9).

Confirmed sales orders consume forecast within the same time bucket so MRP does
not double-count demand. For a forecast line, the remaining (un-consumed)
quantity is what becomes FORECAST demand; the full sales-order quantity is
collected separately as SALES_ORDER demand. Net effect per bucket:

    total demand = sales_orders + max(0, forecast - sales_orders)
                 = max(forecast, sales_orders)

so sales of 70 against a forecast of 100 yields 70 sales + 30 forecast = 100,
and sales of 130 against 100 yields 130 sales + 0 forecast = 130.

Only two methods are implemented this phase:
- NONE         : forecast is never reduced by sales.
- SAME_BUCKET  : sales consume forecast that falls in the same daily / weekly /
                 monthly bucket (per the forecast line's bucket_type).

FORWARD_BACKWARD_DAYS is recognised but unsupported; callers raise
UNSUPPORTED_FORECAST_CONSUMPTION_METHOD and fall back to NONE.
"""
from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError

ZERO = Decimal("0.00")

NONE = "NONE"
SAME_BUCKET = "SAME_BUCKET"
FORWARD_BACKWARD_DAYS = "FORWARD_BACKWARD_DAYS"
SUPPORTED_METHODS = {NONE, SAME_BUCKET}


def bucket_key(d, bucket_type):
    """A hashable key identifying the bucket a date falls in."""
    if bucket_type == "DAILY":
        return ("D", d.toordinal())
    if bucket_type == "WEEKLY":
        iso = d.isocalendar()
        return ("W", iso[0], iso[1])
    return ("M", d.year, d.month)  # MONTHLY (default)


def _confirmed_sales(tenant, product, site, fallback_date):
    """[(sale_date, base_qty)] for confirmed customer orders of product @ site."""
    from core.models import CustomerOrderLine
    from core.services.uom import to_base_qty
    out = []
    lines = (CustomerOrderLine.objects
             .filter(order__tenant=tenant, order__status="CONFIRMED",
                     product=product, order__site=site)
             .select_related("order", "uom"))
    for line in lines:
        try:
            qty = to_base_qty(product, line.qty, line.uom)
        except ValidationError:
            qty = Decimal(line.qty or 0)
        if qty <= ZERO:
            continue
        sale_date = line.order.order_date or fallback_date
        out.append((sale_date, qty))
    return out


def consume(tenant, product, site, lines, method, fallback_date):
    """Apply forecast consumption to ``lines`` (ForecastLine rows for one
    product/site). Returns ``[(line, remaining, consumed)]`` where remaining is
    the forecast demand left after sales consumption."""
    if method == NONE:
        return [(l, max(l.quantity or ZERO, ZERO), ZERO) for l in lines]

    # SAME_BUCKET (the only other supported method; unsupported methods are
    # mapped to NONE by the caller before reaching here).
    sales = _confirmed_sales(tenant, product, site, fallback_date)

    # Sales totals per (bucket_type, key) - a line is consumed only by sales in
    # its own bucket granularity.
    pool = defaultdict(lambda: ZERO)
    for bt in {l.bucket_type for l in lines}:
        for sale_date, qty in sales:
            pool[(bt, bucket_key(sale_date, bt))] += qty

    # Group forecast lines sharing a bucket so a sales unit is consumed once.
    groups = defaultdict(list)
    for l in lines:
        groups[(l.bucket_type, bucket_key(l.forecast_date, l.bucket_type))].append(l)

    results = []
    for group_key, group_lines in groups.items():
        available = pool.get(group_key, ZERO)
        for l in sorted(group_lines, key=lambda x: x.forecast_date):
            fq = max(l.quantity or ZERO, ZERO)
            consumed = min(fq, available) if available > ZERO else ZERO
            available -= consumed
            results.append((l, fq - consumed, consumed))
    return results
