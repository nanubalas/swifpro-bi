"""Labour & overhead absorption for work orders (Phase 13).

Manual booking of labour / overhead hours against a WorkOrderOperation, costed at
the operation's work-centre rate and absorbed into WIP. Each booking is an
append-only WorkOrderCostBooking with its own balanced GL journal (DR WIP / CR
the absorption account) when manufacturing accounting is configured; a missing
profile or account is skipped with a warning (Phase 6/7 behaviour). Reuses the
Phase 7 posting service and the work-order WIP totals.

This phase does not implement booking reversal, payroll, timesheets or live
shop-floor tracking - just the cost absorption needed so finished-goods cost is
material + labour + overhead rather than material only.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from core.services.mrp import work_order_posting as wop
from core.services.mrp.work_order_execution import WorkOrderError, _try_post, INVALID_QUANTITY

ZERO = Decimal("0.00")
COST_DP = Decimal("0.01")

NOT_OPERATIONAL = "WORK_ORDER_NOT_OPERATIONAL"
WO_CLOSED = "WORK_ORDER_CLOSED"
WO_CANCELLED = "WORK_ORDER_CANCELLED"
NO_WORK_CENTRE = "OPERATION_NO_WORK_CENTRE"

LABOUR = "LABOUR"
OVERHEAD = "OVERHEAD"


def _guard(wo):
    if wo.status == "CLOSED":
        raise WorkOrderError(WO_CLOSED, "This work order is closed; labour/overhead cannot be booked.")
    if wo.status == "CANCELLED":
        raise WorkOrderError(WO_CANCELLED, "This work order is cancelled; labour/overhead cannot be booked.")
    if wo.status not in ("RELEASED", "PARTIALLY_COMPLETED"):
        raise WorkOrderError(NOT_OPERATIONAL, "Release the work order before booking labour or overhead.")


def _rate(operation, booking_type):
    wc = operation.work_centre
    if wc is None:
        raise WorkOrderError(NO_WORK_CENTRE,
                             "The operation has no work centre, so no labour/overhead rate is available.")
    return (wc.labour_rate_per_hour if booking_type == LABOUR else wc.overhead_rate_per_hour) or ZERO


def calculate_planned_operation_cost(operation):
    """Planned labour / overhead / total cost for an operation from its planned
    hours and the work-centre rates. Safe when no work centre is set (zeros)."""
    wc = operation.work_centre
    labour_rate = (wc.labour_rate_per_hour if wc else ZERO) or ZERO
    overhead_rate = (wc.overhead_rate_per_hour if wc else ZERO) or ZERO
    labour_hours = operation.planned_labour_hours or operation.planned_hours or ZERO
    overhead_hours = operation.planned_overhead_hours or operation.planned_hours or ZERO
    labour = (labour_hours * labour_rate).quantize(COST_DP, rounding=ROUND_HALF_UP)
    overhead = (overhead_hours * overhead_rate).quantize(COST_DP, rounding=ROUND_HALF_UP)
    return {"labour": labour, "overhead": overhead, "total": labour + overhead}


def _book(operation, hours, user, booking_type, note):
    from core.models import WorkOrderCostBooking
    wo = operation.work_order
    _guard(wo)
    hours = Decimal(hours)
    if hours <= ZERO:
        raise WorkOrderError(INVALID_QUANTITY, "Booked hours must be greater than zero.")
    rate = _rate(operation, booking_type)
    amount = (hours * rate).quantize(COST_DP, rounding=ROUND_HALF_UP)

    booking = WorkOrderCostBooking.objects.create(
        tenant=wo.tenant, work_order=wo, operation=operation, booking_type=booking_type,
        hours=hours, rate_per_hour=rate, amount=amount, status="POSTED",
        booked_by=user, notes=(note or ""))

    if booking_type == LABOUR:
        operation.actual_labour_hours = (operation.actual_labour_hours or ZERO) + hours
        operation.labour_cost = (operation.labour_cost or ZERO) + amount
        operation.save(update_fields=["actual_labour_hours", "labour_cost"])
        wo.wip_labour_cost = (wo.wip_labour_cost or ZERO) + amount
        wo.save(update_fields=["wip_labour_cost"])
        _try_post(wo, lambda: wop.post_work_order_labour(booking, user))
    else:
        operation.actual_overhead_hours = (operation.actual_overhead_hours or ZERO) + hours
        operation.overhead_cost = (operation.overhead_cost or ZERO) + amount
        operation.save(update_fields=["actual_overhead_hours", "overhead_cost"])
        wo.wip_overhead_cost = (wo.wip_overhead_cost or ZERO) + amount
        wo.save(update_fields=["wip_overhead_cost"])
        _try_post(wo, lambda: wop.post_work_order_overhead(booking, user))
    return booking


@transaction.atomic
def book_operation_labour(operation, hours, user, note=None):
    return _book(operation, hours, user, LABOUR, note)


@transaction.atomic
def book_operation_overhead(operation, hours, user, note=None):
    return _book(operation, hours, user, OVERHEAD, note)


@transaction.atomic
def book_operation_actuals(operation, labour_hours, overhead_hours, user, note=None):
    """Book labour and/or overhead in one step. Returns the bookings created."""
    out = []
    if labour_hours and Decimal(labour_hours) > ZERO:
        out.append(_book(operation, labour_hours, user, LABOUR, note))
    if overhead_hours and Decimal(overhead_hours) > ZERO:
        out.append(_book(operation, overhead_hours, user, OVERHEAD, note))
    return out
