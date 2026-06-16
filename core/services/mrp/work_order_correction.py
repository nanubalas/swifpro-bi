"""Manufacturing correction controls (Phase 12): scrap and reversal.

Safe, append-only operational corrections on a work order, reusing the Phase 6
inventory ledger (apply_movement) and the Phase 7 GL posting service:

- scrap component material from WIP            -> WORK_ORDER_SCRAP (audit) + GL
- scrap finished goods during completion       -> WORK_ORDER_COMPLETION_SCRAP + GL
- reverse a material issue (return to stock)    -> WORK_ORDER_ISSUE_REVERSAL + GL
- reverse a completion (remove finished goods)  -> WORK_ORDER_COMPLETION_REVERSAL + GL

Nothing edits or deletes an existing movement: every correction is a new
movement, and the original is marked with the cumulative reversed_quantity so it
can never be over-reversed. GL postings reuse Phase 7 accounts (scrap uses the
manufacturing variance account); a missing profile is skipped with a warning, as
in Phase 6/7. CLOSED and CANCELLED work orders reject all corrections.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from core.services.inventory import apply_movement
from core.services.mrp import work_order_posting as wop
from core.services.mrp.work_order_execution import (
    WorkOrderError, WO_REF_TYPE, _try_post, _location_available, _pick_dest_location,
    _update_material_status, _completion_unit_cost, INVALID_QUANTITY,
)

ZERO = Decimal("0.00")
COST_DP = Decimal("0.01")

# Error codes (surfaced as user messages).
WO_CLOSED = "WORK_ORDER_CLOSED"
WO_CANCELLED = "WORK_ORDER_CANCELLED"
NOT_OPERATIONAL = "WORK_ORDER_NOT_OPERATIONAL"
MISSING_REASON = "CORRECTION_MISSING_REASON"
OVER_SCRAP = "CORRECTION_OVER_SCRAP"
OVER_REVERSAL = "CORRECTION_OVER_REVERSAL"
WRONG_MOVEMENT = "CORRECTION_WRONG_MOVEMENT"
FG_NOT_AVAILABLE = "CORRECTION_FG_NOT_AVAILABLE"
OVER_SCRAP_COMPLETION = "CORRECTION_OVER_SCRAP_COMPLETION"


def _guard_correctable(wo):
    if wo.status == "CLOSED":
        raise WorkOrderError(WO_CLOSED, "This work order is closed; corrections are not allowed.")
    if wo.status == "CANCELLED":
        raise WorkOrderError(WO_CANCELLED, "This work order is cancelled; corrections are not allowed.")


def _require_reason(reason):
    if not (reason or "").strip():
        raise WorkOrderError(MISSING_REASON, "A reason is required for a correction.")
    return reason.strip()


def _q(amount):
    return Decimal(amount or ZERO).quantize(COST_DP, rounding=ROUND_HALF_UP)


def _scrap_location(wo, prefer=None):
    loc = prefer or _pick_dest_location(wo.tenant, wo.site)
    if loc is None:
        raise WorkOrderError("WORK_ORDER_MISSING_DEFAULT_LOCATION",
                             "No stock location at the work order's site to record the correction.")
    return loc


# --------------------------------------------------------------------------- #
# Component material scrap (WIP write-off; stock already left at issue)
# --------------------------------------------------------------------------- #
@transaction.atomic
def scrap_work_order_material(wom, quantity, user, reason):
    from core.models import InventoryMovement
    wo = wom.work_order
    _guard_correctable(wo)
    if wo.status not in ("RELEASED", "PARTIALLY_COMPLETED"):
        raise WorkOrderError(NOT_OPERATIONAL, "Material can only be scrapped on a released work order.")
    reason = _require_reason(reason)
    quantity = Decimal(quantity)
    if quantity <= ZERO:
        raise WorkOrderError(INVALID_QUANTITY, "Scrap quantity must be greater than zero.")

    scrappable = (wom.issued_quantity or ZERO) - (wom.scrapped_quantity or ZERO)
    if quantity > scrappable:
        raise WorkOrderError(
            OVER_SCRAP,
            f"Cannot scrap more than the {scrappable} issued-and-unscrapped of {wom.component.sku}.")

    issued_qty = wom.issued_quantity or ZERO
    unit_cost = ((wom.issued_cost or ZERO) / issued_qty) if issued_qty > ZERO else \
        Decimal(wom.component.standard_cost or ZERO)
    scrap_value = _q(unit_cost * quantity)

    # Append-only audit movement; stock already left inventory at issue, so this
    # carries cost only (qty_delta 0, no balance change).
    movement = InventoryMovement.objects.create(
        tenant=wo.tenant, site=wo.site, product=wom.component,
        location=_scrap_location(wo, prefer=wom.source_location),
        movement_type="WORK_ORDER_SCRAP", user=user, qty_delta=ZERO,
        unit_cost=unit_cost.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), value=-scrap_value,
        ref_type=WO_REF_TYPE, ref_id=wo.work_order_number,
        notes=f"Scrap {quantity} {wom.component.sku}", reversal_reason=reason)

    wom.scrapped_quantity = (wom.scrapped_quantity or ZERO) + quantity
    wom.scrap_cost = (wom.scrap_cost or ZERO) + scrap_value
    wom.save(update_fields=["scrapped_quantity", "scrap_cost"])

    wo.wip_material_cost = (wo.wip_material_cost or ZERO) - scrap_value
    wo.scrap_cost = (wo.scrap_cost or ZERO) + scrap_value
    wo.save(update_fields=["wip_material_cost", "scrap_cost"])

    _try_post(wo, lambda: wop.post_work_order_material_scrap(wo, movement, scrap_value, user))
    return movement


# --------------------------------------------------------------------------- #
# Finished-goods scrap during completion (never enters stock)
# --------------------------------------------------------------------------- #
@transaction.atomic
def scrap_finished_goods(wo, quantity, user, reason):
    from core.models import InventoryMovement
    _guard_correctable(wo)
    if wo.status not in ("RELEASED", "PARTIALLY_COMPLETED"):
        raise WorkOrderError(NOT_OPERATIONAL, "Finished goods can only be scrapped on a released work order.")
    reason = _require_reason(reason)
    quantity = Decimal(quantity)
    if quantity <= ZERO:
        raise WorkOrderError(INVALID_QUANTITY, "Scrap quantity must be greater than zero.")

    used = (wo.quantity_completed or ZERO) + (wo.quantity_scrapped or ZERO)
    if used + quantity > (wo.quantity or ZERO):
        raise WorkOrderError(
            OVER_SCRAP_COMPLETION,
            f"Completed + scrapped cannot exceed the planned {wo.quantity}.")

    unit_cost = _completion_unit_cost(wo)
    scrap_value = _q(unit_cost * quantity)

    movement = InventoryMovement.objects.create(
        tenant=wo.tenant, site=wo.site, product=wo.product,
        location=_scrap_location(wo), movement_type="WORK_ORDER_COMPLETION_SCRAP",
        user=user, qty_delta=ZERO,
        unit_cost=unit_cost.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), value=-scrap_value,
        ref_type=WO_REF_TYPE, ref_id=wo.work_order_number,
        notes=f"Finished-goods scrap {quantity} {wo.product.sku}", reversal_reason=reason)

    wo.quantity_scrapped = (wo.quantity_scrapped or ZERO) + quantity
    wo.wip_material_cost = (wo.wip_material_cost or ZERO) - scrap_value
    wo.scrap_cost = (wo.scrap_cost or ZERO) + scrap_value
    wo.save(update_fields=["quantity_scrapped", "wip_material_cost", "scrap_cost"])

    _try_post(wo, lambda: wop.post_work_order_completion_scrap(wo, movement, scrap_value, user))
    return movement


# --------------------------------------------------------------------------- #
# Reverse a material issue (component goes back into stock)
# --------------------------------------------------------------------------- #
def _remaining_reversible(original):
    return abs(original.qty_delta or ZERO) - (original.reversed_quantity or ZERO)


@transaction.atomic
def reverse_work_order_issue(original, quantity, user, reason):
    wo = _work_order_for(original)
    _guard_correctable(wo)
    reason = _require_reason(reason)
    if original.movement_type != "WORK_ORDER_ISSUE":
        raise WorkOrderError(WRONG_MOVEMENT, "Only a material issue movement can be reversed here.")
    quantity = Decimal(quantity)
    if quantity <= ZERO:
        raise WorkOrderError(INVALID_QUANTITY, "Reversal quantity must be greater than zero.")
    remaining = _remaining_reversible(original)
    if quantity > remaining:
        raise WorkOrderError(OVER_REVERSAL, f"Cannot reverse more than the {remaining} unreversed quantity.")

    wom = wo.materials.filter(component=original.product).first()

    movement = apply_movement(
        tenant=wo.tenant, product=original.product, location=original.location,
        movement_type="WORK_ORDER_ISSUE_REVERSAL", qty_delta=quantity,
        ref_type=WO_REF_TYPE, ref_id=wo.work_order_number,
        notes=f"Reverse issue of {original.product.sku}", unit_cost=original.unit_cost,
        lot_code=original.lot_code, serial_number=original.serial_number,
        expiry_date=original.expiry_date, user=user)
    movement.reversed_movement = original
    movement.is_reversal = True
    movement.reversal_reason = reason
    movement.save(update_fields=["reversed_movement", "is_reversal", "reversal_reason"])

    reversed_value = _q(movement.value or ZERO)
    original.reversed_quantity = (original.reversed_quantity or ZERO) + quantity
    original.save(update_fields=["reversed_quantity"])

    if wom is not None:
        wom.issued_quantity = (wom.issued_quantity or ZERO) - quantity
        wom.issued_cost = (wom.issued_cost or ZERO) - reversed_value
        wom.reversed_quantity = (wom.reversed_quantity or ZERO) + quantity
        _update_material_status(wom)
        wom.save(update_fields=["issued_quantity", "issued_cost", "reversed_quantity", "status"])

    wo.wip_material_cost = (wo.wip_material_cost or ZERO) - reversed_value
    wo.reversal_cost = (wo.reversal_cost or ZERO) + reversed_value
    wo.save(update_fields=["wip_material_cost", "reversal_cost"])

    _try_post(wo, lambda: wop.post_work_order_issue_reversal(wo, movement, reversed_value, user))
    return movement


# --------------------------------------------------------------------------- #
# Reverse a completion (finished goods come out of stock, WIP restored)
# --------------------------------------------------------------------------- #
@transaction.atomic
def reverse_work_order_completion(original, quantity, user, reason):
    wo = _work_order_for(original)
    _guard_correctable(wo)
    reason = _require_reason(reason)
    if original.movement_type != "WORK_ORDER_COMPLETION":
        raise WorkOrderError(WRONG_MOVEMENT, "Only a completion movement can be reversed here.")
    quantity = Decimal(quantity)
    if quantity <= ZERO:
        raise WorkOrderError(INVALID_QUANTITY, "Reversal quantity must be greater than zero.")
    remaining = _remaining_reversible(original)
    if quantity > remaining:
        raise WorkOrderError(OVER_REVERSAL, f"Cannot reverse more than the {remaining} unreversed quantity.")

    # Finished goods must still be available where they were received (not yet
    # shipped, picked, allocated or consumed).
    available = _location_available(wo.tenant, wo.product, original.location)
    if available < quantity:
        raise WorkOrderError(
            FG_NOT_AVAILABLE,
            f"Only {available} of {wo.product.sku} remain at {original.location.name}; "
            f"the rest has been shipped, allocated or consumed.")

    movement = apply_movement(
        tenant=wo.tenant, product=wo.product, location=original.location,
        movement_type="WORK_ORDER_COMPLETION_REVERSAL", qty_delta=-quantity,
        ref_type=WO_REF_TYPE, ref_id=wo.work_order_number,
        notes=f"Reverse completion of {wo.product.sku}",
        lot_code=original.lot_code, serial_number=original.serial_number,
        expiry_date=original.expiry_date, user=user)
    movement.reversed_movement = original
    movement.is_reversal = True
    movement.reversal_reason = reason
    movement.save(update_fields=["reversed_movement", "is_reversal", "reversal_reason"])

    reversed_value = _q(abs(movement.value or ZERO))
    original.reversed_quantity = (original.reversed_quantity or ZERO) + quantity
    original.save(update_fields=["reversed_quantity"])

    wo.quantity_completed = (wo.quantity_completed or ZERO) - quantity
    wo.finished_goods_cost = (wo.finished_goods_cost or ZERO) - reversed_value
    wo.wip_material_cost = (wo.wip_material_cost or ZERO) + reversed_value
    wo.reversal_cost = (wo.reversal_cost or ZERO) + reversed_value
    if wo.quantity_completed <= ZERO:
        wo.status = "RELEASED"
        wo.completed_at = None
    elif wo.quantity_completed < (wo.quantity or ZERO):
        wo.status = "PARTIALLY_COMPLETED"
        wo.completed_at = None
    wo.save(update_fields=["quantity_completed", "finished_goods_cost", "wip_material_cost",
                           "reversal_cost", "status", "completed_at"])

    _try_post(wo, lambda: wop.post_work_order_completion_reversal(wo, movement, reversed_value, user))
    return movement


def _work_order_for(movement):
    from core.models import WorkOrder
    wo = WorkOrder.objects.filter(tenant=movement.tenant, work_order_number=movement.ref_id).first()
    if wo is None:
        raise WorkOrderError(WRONG_MOVEMENT, "This movement is not linked to a work order.")
    return wo
