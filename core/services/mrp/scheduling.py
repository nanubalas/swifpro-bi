"""Finite scheduling foundation + shop-calendar capacity (Phase 14).

A safe, deterministic scheduling layer on top of the Phase 10 rough-cut capacity.
Calendars define which days are working days and the daily capacity; the
scheduler places routing/work-order operation hours into those days (backward
from a required date, or forward from a start date), skipping non-working days
and - for finite work centres - respecting the load already committed by other
work-order operations.

This is NOT full APS: no optimisation, no operation splitting into segments
(an operation simply spans the days it needs via start/end dates), and automatic
levelling is deferred. When a work centre has no calendar the scheduler falls
back to Monday-Friday at the centre's daily capacity.
"""
import datetime
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

ZERO = Decimal("0.00")
ONE_DAY = datetime.timedelta(days=1)
_DEFAULT_HOURS_PER_DAY = Decimal("8")
_MAX_HORIZON_DAYS = 730  # 2 years either side - guards the day-walk loops
_OPEN_WO_STATUSES = ["PLANNED", "FIRM", "RELEASED", "PARTIALLY_COMPLETED"]
_DEFAULT_WORKING_WEEKDAYS = {0, 1, 2, 3, 4}  # Mon-Fri


class SchedulingError(Exception):
    """Raised when an operation cannot be placed within the horizon. ``code`` is
    one of the MRPException codes used by the caller."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _base_capacity(work_centre):
    cap = work_centre.capacity_hours_per_day if work_centre else None
    return Decimal(cap) if cap and cap > 0 else _DEFAULT_HOURS_PER_DAY


# --------------------------------------------------------------------------- #
# Calendar capacity
# --------------------------------------------------------------------------- #
def get_working_capacity(work_centre, date):
    """Available hours at ``work_centre`` on ``date`` from its calendar (or the
    Mon-Fri fallback). Holidays/shutdowns return 0; reduced/extra shifts scale the
    base daily capacity by the exception multiplier."""
    base = _base_capacity(work_centre)
    cal = getattr(work_centre, "shop_calendar", None)
    if cal is None:
        return base if date.weekday() in _DEFAULT_WORKING_WEEKDAYS else ZERO

    exc = cal.exceptions.filter(date=date).first()
    if exc is not None:
        return (base * Decimal(exc.capacity_multiplier or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    wd = cal.working_days.filter(weekday=date.weekday()).first()
    if wd is None:
        # Unconfigured weekday: fall back to the Mon-Fri default at full capacity.
        return base if date.weekday() in _DEFAULT_WORKING_WEEKDAYS else ZERO
    if not wd.is_working_day:
        return ZERO
    return (base * Decimal(wd.capacity_multiplier or 1)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def is_working_day(work_centre, date):
    return get_working_capacity(work_centre, date) > ZERO


def next_working_day(work_centre, date):
    cursor, guard = date + ONE_DAY, 0
    while guard < _MAX_HORIZON_DAYS:
        if is_working_day(work_centre, cursor):
            return cursor
        cursor += ONE_DAY
        guard += 1
    return date + ONE_DAY


def previous_working_day(work_centre, date):
    cursor, guard = date - ONE_DAY, 0
    while guard < _MAX_HORIZON_DAYS:
        if is_working_day(work_centre, cursor):
            return cursor
        cursor -= ONE_DAY
        guard += 1
    return date - ONE_DAY


def _working_days_between(work_centre, start, end):
    days, cursor, guard = [], start, 0
    while cursor <= end and guard < _MAX_HORIZON_DAYS:
        if is_working_day(work_centre, cursor):
            days.append(cursor)
        cursor += ONE_DAY
        guard += 1
    return days


# --------------------------------------------------------------------------- #
# Committed load (from existing work-order operations)
# --------------------------------------------------------------------------- #
def daily_scheduled_hours(work_centre, date, exclude_op_id=None):
    """Hours already committed at ``work_centre`` on ``date`` by open work-order
    operations, distributing each operation's planned hours evenly over the
    working days it spans."""
    from core.models import WorkOrderOperation
    total = ZERO
    ops = (WorkOrderOperation.objects
           .filter(work_centre=work_centre, work_order__status__in=_OPEN_WO_STATUSES,
                   planned_start__isnull=False, planned_end__isnull=False,
                   planned_start__lte=date, planned_end__gte=date)
           .exclude(id=exclude_op_id) if exclude_op_id else
           WorkOrderOperation.objects.filter(
               work_centre=work_centre, work_order__status__in=_OPEN_WO_STATUSES,
               planned_start__isnull=False, planned_end__isnull=False,
               planned_start__lte=date, planned_end__gte=date))
    for op in ops:
        span = _working_days_between(work_centre, op.planned_start, op.planned_end)
        if date in span:
            total += (Decimal(op.planned_hours or 0) / Decimal(len(span)))
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
# Operation placement
# --------------------------------------------------------------------------- #
def schedule_operation_backward(work_centre, hours, required_end, exclude_op_id=None):
    """Place ``hours`` into working days ending on/before ``required_end``,
    walking backward. Returns (start_date, end_date). Raises SchedulingError when
    the hours cannot be placed within the horizon."""
    hours = Decimal(hours or 0)
    if hours <= ZERO:
        return required_end, required_end
    finite = bool(work_centre and work_centre.finite_capacity_enabled)
    remaining, cursor, guard = hours, required_end, 0
    start_day = end_day = None
    while remaining > ZERO and guard < _MAX_HORIZON_DAYS:
        cap = get_working_capacity(work_centre, cursor)
        if cap > ZERO:
            avail = cap - daily_scheduled_hours(work_centre, cursor, exclude_op_id) if finite else cap
            if avail > ZERO:
                remaining -= min(remaining, avail)
                end_day = end_day or cursor
                start_day = cursor
        cursor -= ONE_DAY
        guard += 1
    if remaining > ZERO:
        raise SchedulingError("NO_WORKING_CAPACITY",
                              "No working capacity available to place the operation in the horizon.")
    return start_day, end_day


def schedule_operation_forward(work_centre, hours, earliest_start, exclude_op_id=None):
    """Place ``hours`` into working days starting on/after ``earliest_start``,
    walking forward. Returns (start_date, end_date)."""
    hours = Decimal(hours or 0)
    if hours <= ZERO:
        return earliest_start, earliest_start
    finite = bool(work_centre and work_centre.finite_capacity_enabled)
    remaining, cursor, guard = hours, earliest_start, 0
    start_day = end_day = None
    while remaining > ZERO and guard < _MAX_HORIZON_DAYS:
        cap = get_working_capacity(work_centre, cursor)
        if cap > ZERO:
            avail = cap - daily_scheduled_hours(work_centre, cursor, exclude_op_id) if finite else cap
            if avail > ZERO:
                remaining -= min(remaining, avail)
                start_day = start_day or cursor
                end_day = cursor
        cursor += ONE_DAY
        guard += 1
    if remaining > ZERO:
        raise SchedulingError("NO_WORKING_CAPACITY",
                              "No working capacity available to place the operation in the horizon.")
    return start_day, end_day


def _op_hours(op):
    """Effective hours for a work-order operation (reuses Phase 13 planned hours,
    else the Phase 10 duration calc on its source routing operation)."""
    if op.planned_hours and op.planned_hours > 0:
        return Decimal(op.planned_hours)
    from core.services.mrp.routing_capacity import calculate_operation_hours
    src = op.source_routing_operation
    return calculate_operation_hours(src, op.work_order.quantity) if src else ZERO


def should_finite_schedule(routing):
    """True when at least one internal operation's work centre opts into
    scheduling (has a calendar or finite capacity). Without this, Phase 10
    rough-cut behaviour is preserved unchanged."""
    for op in routing.operations.select_related("work_centre").all():
        if op.is_subcontract_operation:
            continue
        wc = op.work_centre
        if wc and (wc.shop_calendar_id or wc.finite_capacity_enabled):
            return True
    return False


# --------------------------------------------------------------------------- #
# Work-order scheduling
# --------------------------------------------------------------------------- #
def schedule_work_order_operations(work_order, mode="BACKWARD"):
    """Set planned_start / planned_end on each operation (and the work order),
    skipping non-working days and respecting finite capacity. Returns the
    operations. Raises SchedulingError if placement fails."""
    ops = list(work_order.operations.select_related("work_centre").order_by("operation_sequence"))
    if not ops:
        return ops

    if mode == "FORWARD":
        cursor = work_order.planned_start_date or work_order.required_date or work_order.created_at.date()
        for op in ops:
            wc = op.work_centre
            if wc is None or (op.source_routing_operation and op.source_routing_operation.is_subcontract_operation):
                op.planned_start = op.planned_end = cursor
            else:
                start = cursor if is_working_day(wc, cursor) else next_working_day(wc, cursor)
                op.planned_start, op.planned_end = schedule_operation_forward(wc, _op_hours(op), start, op.id)
                cursor = next_working_day(wc, op.planned_end)
            op.save(update_fields=["planned_start", "planned_end"])
    else:  # BACKWARD
        cursor = work_order.planned_end_date or work_order.required_date
        if cursor is None:
            raise SchedulingError("SCHEDULING_FAILED", "The work order has no required/end date to schedule from.")
        for op in reversed(ops):
            wc = op.work_centre
            if wc is None or (op.source_routing_operation and op.source_routing_operation.is_subcontract_operation):
                op.planned_start = op.planned_end = cursor
                cursor = cursor - ONE_DAY
            else:
                end = cursor if is_working_day(wc, cursor) else previous_working_day(wc, cursor)
                op.planned_start, op.planned_end = schedule_operation_backward(wc, _op_hours(op), end, op.id)
                cursor = previous_working_day(wc, op.planned_start)
            op.save(update_fields=["planned_start", "planned_end"])

    starts = [o.planned_start for o in ops if o.planned_start]
    ends = [o.planned_end for o in ops if o.planned_end]
    if starts and ends:
        work_order.planned_start_date = min(starts)
        work_order.planned_end_date = max(ends)
        work_order.save(update_fields=["planned_start_date", "planned_end_date"])
    return ops


def schedule_mrp_planned_order(planned_order, routing, mode="BACKWARD"):
    """Backward-schedule a MAKE planned order's routing to derive release/receipt
    dates from a finite schedule. Returns (release, receipt). Raises
    SchedulingError on failure (the caller falls back to Phase 10 duration)."""
    from core.services.mrp.routing_capacity import calculate_operation_hours
    ops = [op for op in routing.operations.select_related("work_centre").order_by("operation_sequence")
           if not op.is_subcontract_operation]
    receipt = planned_order.planned_receipt_date or planned_order.required_date
    if receipt is None or not ops:
        raise SchedulingError("SCHEDULING_FAILED", "No required date or no internal operations to schedule.")

    cursor = receipt
    starts, ends = [], []
    for op in reversed(ops):
        wc = op.work_centre
        hours = calculate_operation_hours(op, planned_order.quantity)
        if wc is None:
            starts.append(cursor)
            ends.append(cursor)
            cursor = cursor - ONE_DAY
            continue
        end = cursor if is_working_day(wc, cursor) else previous_working_day(wc, cursor)
        start, end = schedule_operation_backward(wc, hours, end)
        starts.append(start)
        ends.append(end)
        cursor = previous_working_day(wc, start)
    release = min(starts)
    receipt_actual = max(ends)
    return release, receipt_actual


# --------------------------------------------------------------------------- #
# Capacity load view
# --------------------------------------------------------------------------- #
def calculate_daily_load(work_centre, start_date, end_date):
    """Per-day {date, available, scheduled, remaining, overload, utilisation} for
    a work centre across [start_date, end_date]."""
    rows, cursor, guard = [], start_date, 0
    while cursor <= end_date and guard < _MAX_HORIZON_DAYS:
        avail = get_working_capacity(work_centre, cursor)
        sched = daily_scheduled_hours(work_centre, cursor)
        remaining = (avail - sched) if avail > sched else ZERO
        overload = (sched - avail) if sched > avail else ZERO
        util = (sched / avail * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) \
            if avail > ZERO else (Decimal("100.0") if sched > ZERO else ZERO)
        rows.append({"date": cursor, "available": avail, "scheduled": sched,
                     "remaining": remaining, "overload": overload, "utilisation": util})
        cursor += ONE_DAY
        guard += 1
    return rows


def level_work_centre_load(work_centre, start_date, end_date):
    """Capacity levelling is DEFERRED this phase (it would move planned orders).
    Returned for API completeness; performs no changes and reports the overloaded
    days so a planner can act manually."""
    overloaded = [r for r in calculate_daily_load(work_centre, start_date, end_date) if r["overload"] > ZERO]
    return {"moved": 0, "deferred": True, "overloaded_days": overloaded}
