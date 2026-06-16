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

    # Forecast demand (Phase 9), net of same-bucket sales consumption.
    if run.include_forecast and profile.include_forecast:
        version = resolve_forecast_version(run)
        if version is not None:
            created.extend(_collect_forecast(run, profile, version))

    return created


def resolve_forecast_version(run):
    """Resolve and validate the run's selected forecast version exactly once
    (cached on the run). Raises FORECAST_VERSION_MISSING / _INVALID exceptions as
    needed and returns the usable ForecastVersion or None. The version is never
    fatal: the run always continues."""
    from core.models import ForecastVersion

    cached = getattr(run, "_forecast_version_resolved", "unset")
    if cached != "unset":
        return cached

    version = None
    if run.include_forecast:
        ident = (run.forecast_version or "").strip()
        if not ident:
            mrp_exc.raise_exception(
                run, "FORECAST_VERSION_MISSING",
                "Forecast is included but no forecast version was selected; "
                "forecast demand was skipped.",
                severity="WARNING", dedupe_key=("fc_missing",))
        else:
            v = None
            if ident.isdigit():
                v = ForecastVersion.objects.filter(tenant=run.tenant, pk=int(ident)).first()
            if v is None:
                v = ForecastVersion.objects.filter(tenant=run.tenant, code=ident).first()
            if v is None:
                mrp_exc.raise_exception(
                    run, "FORECAST_VERSION_INVALID",
                    f"Forecast version '{ident}' was not found; forecast demand was skipped.",
                    severity="WARNING", dedupe_key=("fc_invalid",))
            elif not v.is_selectable_for_mrp:
                mrp_exc.raise_exception(
                    run, "FORECAST_VERSION_INVALID",
                    f"Forecast version {v.code} is {v.get_status_display()} and cannot be used "
                    f"for planning; forecast demand was skipped.",
                    severity="WARNING", dedupe_key=("fc_invalid",))
            else:
                version = v

    run._forecast_version_resolved = version
    return version


def _collect_forecast(run, profile, version):
    """Create FORECAST MRPDemand rows for one profile from a forecast version,
    after consuming forecast with confirmed sales orders in the same bucket."""
    from core.models import ForecastLine, MRPDemand
    from core.services.mrp import forecast_consumption as fc

    tenant = run.tenant
    product = profile.product
    site = profile.site
    start = run.planning_start_date
    end = run.planning_end_date
    created = []

    qs = ForecastLine.objects.filter(
        tenant=tenant, forecast_version=version, product=product, site=site,
        forecast_date__gte=start)
    if end:
        qs = qs.filter(forecast_date__lte=end)
    lines = list(qs.select_related("product"))
    if not lines:
        return created

    method = version.consumption_method
    if method not in fc.SUPPORTED_METHODS:
        mrp_exc.raise_exception(
            run, "UNSUPPORTED_FORECAST_CONSUMPTION_METHOD",
            f"Forecast consumption method {version.get_consumption_method_display()} is not "
            f"supported yet; forecast was not consumed by sales.",
            product=product, site=site, dedupe_key=("fc_method", version.id))
        method = fc.NONE

    for line, remaining, consumed in fc.consume(tenant, product, site, lines, method, start):
        if (line.quantity or ZERO) < ZERO:
            mrp_exc.raise_exception(
                run, "FORECAST_LINE_INVALID_QTY",
                f"Forecast line for {product.sku} at {site.name} has a negative quantity; skipped.",
                product=product, site=site, dedupe_key=("fc_qty", line.id))
            continue
        if remaining <= ZERO:
            continue
        created.append(MRPDemand.objects.create(
            mrp_run=run, tenant=tenant, product=product, site=site,
            demand_type="FORECAST",
            source_document_type="ForecastVersion", source_document_id=str(version.id),
            source_line_id=str(line.id),
            required_date=line.forecast_date, quantity=remaining,
            consumed_quantity=consumed, open_quantity=remaining,
            priority=0,
        ))
    return created
