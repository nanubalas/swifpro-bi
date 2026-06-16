"""Routing duration + rough-cut work-centre capacity for MAKE items (Phase 10).

This is *rough-cut* capacity planning, not finite scheduling: durations are
computed from routing operation times, MAKE planned-order release dates are
offset by the routing duration (in whole days), and work-centre load is summed
per day and compared with daily capacity. Nothing here moves other orders,
books labour, or posts GL.

Duration per operation (effective hours):

    eff_qty       = quantity / (yield_percent / 100)          # yield inflation
    minutes       = setup + run_per_unit * eff_qty + queue + move
    hours         = minutes / 60
    effective     = hours / (efficiency_percent / 100)         # work-centre efficiency

Efficiency is applied to *required* hours (above); to avoid double-counting it
is NOT re-applied to available capacity - a work centre's available hours are
simply ``capacity_hours_per_day``.

Capacity overload: for each (work centre, day), if summed required hours exceed
available hours a CAPACITY_OVERLOAD exception is raised (CRITICAL when the
overload is >= 50% of capacity, otherwise WARNING).
"""
import datetime
from collections import defaultdict
from decimal import Decimal, ROUND_CEILING

from django.db.models import Q

from core.services.mrp import exceptions as mrp_exc

ZERO = Decimal("0.00")
HOURS_DP = Decimal("0.01")
ONE = Decimal("1")
HUNDRED = Decimal("100")
_DEFAULT_HOURS_PER_DAY = Decimal("8")

_OPEN_WO_STATUSES = ["PLANNED", "FIRM", "RELEASED", "PARTIALLY_COMPLETED"]


# --------------------------------------------------------------------------- #
# Routing selection
# --------------------------------------------------------------------------- #
def find_active_routing(product, site, required_date=None):
    """The active routing to use for ``product`` at ``site``. A site-specific
    routing wins over a global (site=null) one; within a scope, prefer the
    default, then the lowest routing_code, then the highest revision. Effectivity
    dates are respected when set."""
    from core.models import RoutingHeader

    base = RoutingHeader.objects.filter(product=product, status="ACTIVE")
    if required_date is not None:
        base = base.filter(
            (Q(effective_from__isnull=True) | Q(effective_from__lte=required_date)) &
            (Q(effective_to__isnull=True) | Q(effective_to__gte=required_date)))
    for scope in (base.filter(site=site), base.filter(site__isnull=True)):
        routing = scope.order_by("-is_default", "routing_code", "-revision").first()
        if routing is not None:
            return routing
    return None


# --------------------------------------------------------------------------- #
# Duration
# --------------------------------------------------------------------------- #
def calculate_operation_hours(operation, quantity):
    """Effective hours for one routing/work-order operation to make ``quantity``
    (setup + run + queue + move, yield-inflated, divided by work-centre
    efficiency). Rounded up to 2dp."""
    qty = Decimal(quantity or 0)
    op_yield = Decimal(operation.yield_percent or HUNDRED)
    if op_yield <= ZERO:
        op_yield = HUNDRED
    eff_qty = qty / (op_yield / HUNDRED)

    setup = Decimal(operation.setup_minutes or 0)
    run = Decimal(operation.run_minutes_per_unit or 0) * eff_qty
    queue = Decimal(getattr(operation, "queue_minutes", 0) or 0)
    move = Decimal(getattr(operation, "move_minutes", 0) or 0)
    hours = (setup + run + queue + move) / Decimal(60)

    wc = operation.work_centre
    efficiency = Decimal(getattr(wc, "efficiency_percent", HUNDRED) or HUNDRED) if wc else HUNDRED
    if efficiency <= ZERO:
        efficiency = HUNDRED
    effective = hours / (efficiency / HUNDRED)
    return effective.quantize(HOURS_DP, rounding=ROUND_CEILING)


def calculate_routing_duration(routing, quantity):
    """Total effective hours across all of a routing's operations."""
    total = ZERO
    for op in routing.operations.select_related("work_centre").all():
        total += calculate_operation_hours(op, quantity)
    return total


def _hours_per_day(work_centre):
    if work_centre and (work_centre.capacity_hours_per_day or 0) > 0:
        return Decimal(work_centre.capacity_hours_per_day)
    return _DEFAULT_HOURS_PER_DAY


def _hours_to_days(hours, per_day):
    if per_day <= ZERO:
        per_day = _DEFAULT_HOURS_PER_DAY
    days = (hours / per_day).quantize(ONE, rounding=ROUND_CEILING)
    return max(int(days), 1)


# --------------------------------------------------------------------------- #
# Engine integration: routing-based release date for a MAKE planned order
# --------------------------------------------------------------------------- #
def apply_routing_schedule(run, profile, po):
    """For a MAKE planned order, replace the lead-time release date with one
    derived from routing duration. Missing/invalid routing or work centres raise
    exceptions and leave the lead-time release in place. Returns the routing used
    (or None)."""
    from core.models import RoutingHeader

    product, site = profile.product, profile.site
    routing = find_active_routing(product, site, po.required_date)
    if routing is None:
        exists = RoutingHeader.objects.filter(product=product).exists()
        if exists:
            mrp_exc.raise_exception(
                run, "ROUTING_NOT_ACTIVE",
                f"No active routing for make item {product.sku} at {site.name}; lead time used.",
                product=product, site=site, planned_order=po,
                dedupe_key=("routing_inactive", profile.id))
        else:
            mrp_exc.raise_exception(
                run, "MISSING_ROUTING",
                f"No routing for make item {product.sku} at {site.name}; lead time used.",
                product=product, site=site, planned_order=po,
                dedupe_key=("routing_missing", profile.id))
        mrp_exc.bump_level(po, "WARNING")
        return None

    ops = list(routing.operations.select_related("work_centre").all())
    if not ops:
        mrp_exc.raise_exception(
            run, "INVALID_ROUTING",
            f"Routing {routing.routing_code} for {product.sku} has no operations; lead time used.",
            product=product, site=site, planned_order=po, dedupe_key=("routing_invalid", routing.id))
        mrp_exc.bump_level(po, "WARNING")
        return None

    _validate_operations(run, routing, profile, po, ops)

    duration = calculate_routing_duration(routing, po.quantity)
    if duration <= ZERO:
        mrp_exc.raise_exception(
            run, "ROUTING_DURATION_INVALID",
            f"Routing {routing.routing_code} for {product.sku} produced a non-positive duration.",
            product=product, site=site, planned_order=po, dedupe_key=("routing_dur", routing.id))
        return routing

    caps = [_hours_per_day(op.work_centre) for op in ops if op.work_centre and op.work_centre.is_active]
    per_day = min(caps) if caps else _DEFAULT_HOURS_PER_DAY
    days = _hours_to_days(duration, per_day)
    receipt = po.planned_receipt_date or po.required_date
    po.planned_release_date = receipt - datetime.timedelta(days=days)
    po.save(update_fields=["planned_release_date"])
    return routing


def _validate_operations(run, routing, profile, po, ops):
    product, site = profile.product, profile.site
    for op in ops:
        if op.is_subcontract_operation:
            mrp_exc.raise_exception(
                run, "SUBCONTRACT_OPERATION_NOT_SUPPORTED",
                f"Operation {op.operation_sequence} of {product.sku} is a subcontract step; "
                f"not scheduled this phase.",
                product=product, site=site, planned_order=po, dedupe_key=("subop", op.id))
        wc = op.work_centre
        if wc is None:
            mrp_exc.raise_exception(
                run, "MISSING_WORK_CENTRE",
                f"Operation {op.operation_sequence} of routing {routing.routing_code} has no work centre.",
                product=product, site=site, planned_order=po, dedupe_key=("wc_missing", op.id))
            mrp_exc.bump_level(po, "WARNING")
        elif not wc.is_active:
            mrp_exc.raise_exception(
                run, "INVALID_WORK_CENTRE",
                f"Work centre {wc.code} for {product.sku} is inactive.",
                product=product, site=site, planned_order=po, dedupe_key=("wc_invalid", wc.id))
            mrp_exc.bump_level(po, "WARNING")


# --------------------------------------------------------------------------- #
# Rough-cut capacity load + overload detection
# --------------------------------------------------------------------------- #
def calculate_work_centre_load(run):
    """Rough-cut required hours per (work_centre_id, date) from this run's MAKE
    planned orders and existing open work orders that have operation lines.
    Returns ``(load_dict, work_centre_map)``."""
    from core.models import MRPPlannedOrder, WorkOrder

    load = defaultdict(lambda: ZERO)
    wc_map = {}

    pos = (MRPPlannedOrder.objects.filter(mrp_run=run, source_type="MAKE")
           .select_related("product", "site"))
    for po in pos:
        routing = find_active_routing(po.product, po.site, po.required_date)
        if routing is None:
            continue
        day = po.planned_receipt_date or po.required_date
        if day is None:
            continue
        for op in routing.operations.select_related("work_centre").all():
            wc = op.work_centre
            if wc is None or not wc.is_active:
                continue
            load[(wc.id, day)] += calculate_operation_hours(op, po.quantity)
            wc_map[wc.id] = wc

    wos = (WorkOrder.objects.filter(tenant=run.tenant, status__in=_OPEN_WO_STATUSES)
           .prefetch_related("operations__work_centre"))
    for wo in wos:
        for op in wo.operations.all():
            wc = op.work_centre
            if wc is None or not wc.is_active:
                continue
            day = op.planned_end or op.planned_start or wo.planned_end_date or wo.required_date
            if day is None:
                continue
            load[(wc.id, day)] += (op.planned_hours or ZERO)
            wc_map[wc.id] = wc

    return load, wc_map


def detect_capacity_overloads(run):
    """Raise a CAPACITY_OVERLOAD exception for each overloaded (work centre, day).
    Returns the number of overloads found."""
    load, wc_map = calculate_work_centre_load(run)
    overloads = 0
    for (wc_id, day), required in load.items():
        wc = wc_map.get(wc_id)
        if wc is None:
            continue
        available = wc.available_hours_per_day
        if available <= ZERO or required <= available:
            continue
        over = (required - available).quantize(HOURS_DP)
        pct = (over / available * HUNDRED) if available > ZERO else HUNDRED
        severity = "CRITICAL" if pct >= Decimal("50") else "WARNING"
        mrp_exc.raise_exception(
            run, "CAPACITY_OVERLOAD",
            f"{wc.name} overloaded by {over} hours on {day} "
            f"(load {required.quantize(HOURS_DP)}h vs {available.quantize(HOURS_DP)}h available).",
            site=wc.site, severity=severity, dedupe_key=("cap", wc_id, day))
        overloads += 1
    return overloads


# --------------------------------------------------------------------------- #
# Work-order operation creation (conversion integration)
# --------------------------------------------------------------------------- #
def create_work_order_operations(work_order, routing):
    """Create WorkOrderOperation rows from ``routing``, scheduled backwards from
    the work order's planned end date (reverse operation sequence). Returns the
    created operations (forward order)."""
    from core.models import WorkOrderOperation

    if routing is None:
        return []
    ops = list(routing.operations.select_related("work_centre").order_by("operation_sequence"))
    if not ops:
        return []

    cursor_end = work_order.planned_end_date or work_order.required_date
    scheduled = []  # (op, hours, start, end) in reverse sequence
    for op in reversed(ops):
        hours = calculate_operation_hours(op, work_order.quantity)
        start = end = None
        if cursor_end is not None:
            days = _hours_to_days(hours, _hours_per_day(op.work_centre))
            end = cursor_end
            start = cursor_end - datetime.timedelta(days=days)
            cursor_end = start
        scheduled.append((op, hours, start, end))

    created = []
    for op, hours, start, end in reversed(scheduled):
        created.append(WorkOrderOperation.objects.create(
            work_order=work_order, operation_sequence=op.operation_sequence,
            operation_name=op.operation_name, work_centre=op.work_centre,
            setup_minutes=op.setup_minutes, run_minutes_per_unit=op.run_minutes_per_unit,
            planned_hours=hours, planned_start=start, planned_end=end,
            status="PLANNED", source_routing_operation=op, notes=op.notes))
    return created
