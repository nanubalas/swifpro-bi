"""Work order execution (Phase 6): a safe, basic make-to-stock flow.

firm -> release -> issue materials -> complete finished goods -> close.

Reuses the existing append-only inventory ledger (apply_movement) for both the
component issue (stock out, valued at current cost) and the finished-good
completion (stock in, valued at the work order's accumulated material cost, or
the product standard cost when no material was issued). Basic WIP costing is
tracked on the work order (wip_material_cost / finished_goods_cost).

GL / WIP journal posting is DEFERRED in this phase (no existing WIP posting
helper) - costs are stored on the documents only. No labour, overhead, routing,
capacity, or scrap movement here.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from core.services.inventory import apply_movement
from core.services.mrp.inventory_snapshot import NON_NETTABLE_LOCATION_TYPES

ZERO = Decimal("0.00")
COST_DP = Decimal("0.01")

WO_REF_TYPE = "WORK_ORDER"

# Error codes (surfaced as user messages).
NOT_RELEASED = "WORK_ORDER_NOT_RELEASED"
INVALID_STATUS = "WORK_ORDER_INVALID_STATUS"
INVALID_QUANTITY = "WORK_ORDER_INVALID_QUANTITY"
OVER_ISSUE = "WORK_ORDER_OVER_ISSUE"
INSUFFICIENT_STOCK = "WORK_ORDER_INSUFFICIENT_STOCK"
NON_NETTABLE_LOCATION = "WORK_ORDER_NON_NETTABLE_LOCATION"
OVER_COMPLETION = "WORK_ORDER_OVER_COMPLETION"
MISSING_DEFAULT_LOCATION = "WORK_ORDER_MISSING_DEFAULT_LOCATION"
CANNOT_CANCEL = "WORK_ORDER_CANNOT_CANCEL"
CANNOT_CLOSE = "WORK_ORDER_CANNOT_CLOSE"
MATERIALS_NOT_ISSUED = "WORK_ORDER_MATERIALS_NOT_ISSUED"


class WorkOrderError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


# --------------------------------------------------------------------------- #
# Location helpers
# --------------------------------------------------------------------------- #
def _location_available(tenant, product, location):
    from core.models import InventoryBalance
    b = InventoryBalance.objects.filter(tenant=tenant, product=product, location=location).first()
    if b is None:
        return ZERO
    return (b.on_hand or ZERO) - (b.reserved or ZERO)


def _is_nettable(location):
    return (location.is_active and location.holds_stock
            and location.type not in NON_NETTABLE_LOCATION_TYPES)


def _pick_source_location(tenant, product, site, needed, prefer=None):
    """A nettable location at ``site`` with at least ``needed`` available, the
    preferred one first."""
    from core.models import Location
    candidates = []
    if prefer is not None and prefer.site_id == site.id and _is_nettable(prefer):
        candidates.append(prefer)
    for loc in (Location.objects.filter(tenant=tenant, site=site, is_active=True, holds_stock=True)
                .exclude(type__in=NON_NETTABLE_LOCATION_TYPES).order_by("id")):
        if loc.id not in [c.id for c in candidates]:
            candidates.append(loc)
    for loc in candidates:
        if _location_available(tenant, product, loc) >= needed:
            return loc
    return None


def _best_source_location(tenant, product, site, prefer=None):
    """The nettable location at ``site`` with the most available (for issue-all)."""
    from core.models import Location
    best, best_qty = None, ZERO
    locs = list(Location.objects.filter(tenant=tenant, site=site, is_active=True, holds_stock=True)
                .exclude(type__in=NON_NETTABLE_LOCATION_TYPES).order_by("id"))
    if prefer is not None and prefer.site_id == site.id and _is_nettable(prefer):
        locs = [prefer] + [l for l in locs if l.id != prefer.id]
    for loc in locs:
        avail = _location_available(tenant, product, loc)
        if avail > best_qty:
            best, best_qty = loc, avail
    return best, best_qty


def _pick_dest_location(tenant, site, prefer=None):
    from core.models import Location
    if prefer is not None and prefer.site_id == site.id and _is_nettable(prefer):
        return prefer
    return (Location.objects.filter(tenant=tenant, site=site, is_active=True, holds_stock=True)
            .exclude(type__in=NON_NETTABLE_LOCATION_TYPES).order_by("id").first())


# --------------------------------------------------------------------------- #
# Status transitions
# --------------------------------------------------------------------------- #
def firm_work_order(wo, user):
    if wo.status == "FIRM":
        return wo
    if wo.status != "PLANNED":
        raise WorkOrderError(INVALID_STATUS, f"Only a Planned work order can be firmed (is {wo.get_status_display()}).")
    wo.status = "FIRM"
    wo.save(update_fields=["status"])
    return wo


def release_work_order(wo, user):
    if wo.status == "RELEASED":
        return wo
    if wo.status not in ("PLANNED", "FIRM"):
        raise WorkOrderError(INVALID_STATUS, f"Only a Planned or Firm work order can be released (is {wo.get_status_display()}).")
    wo.status = "RELEASED"
    wo.released_by = user
    wo.released_at = timezone.now()
    if wo.actual_start_date is None:
        wo.actual_start_date = timezone.localdate()
    wo.save(update_fields=["status", "released_by", "released_at", "actual_start_date"])
    return wo


def cancel_work_order(wo, user):
    issued = any((m.issued_quantity or ZERO) > ZERO for m in wo.materials.all())
    if (wo.quantity_completed or ZERO) > ZERO or issued:
        raise WorkOrderError(CANNOT_CANCEL, "Cannot cancel a work order once material has been issued or units completed.")
    if wo.status not in ("PLANNED", "FIRM", "RELEASED"):
        raise WorkOrderError(INVALID_STATUS, f"Cannot cancel a {wo.get_status_display()} work order.")
    wo.status = "CANCELLED"
    wo.save(update_fields=["status"])
    return wo


def close_work_order(wo, user):
    if wo.status == "CLOSED":
        return wo
    if wo.status != "COMPLETED":
        raise WorkOrderError(CANNOT_CLOSE, "A work order can only be closed once it is fully completed.")
    wo.status = "CLOSED"
    wo.closed_by = user
    wo.closed_at = timezone.now()
    wo.save(update_fields=["status", "closed_by", "closed_at"])
    return wo


# --------------------------------------------------------------------------- #
# Material issue
# --------------------------------------------------------------------------- #
def _update_material_status(wom):
    if wom.issued_quantity <= ZERO:
        wom.status = "OPEN"
    elif wom.issued_quantity < wom.required_quantity:
        wom.status = "PARTIALLY_ISSUED"
    else:
        wom.status = "ISSUED"


@transaction.atomic
def issue_material(wom, quantity, user, location=None, bin=None,
                   lot_code=None, serial_number=None, expiry_date=None):
    """Issue ``quantity`` of a component into the work order. Reduces component
    stock via an append-only WORK_ORDER_ISSUE movement; never partial unless the
    caller asked for less than the remaining requirement."""
    wo = wom.work_order
    if wo.status not in ("RELEASED", "PARTIALLY_COMPLETED"):
        raise WorkOrderError(NOT_RELEASED, "Release the work order before issuing materials.")
    quantity = Decimal(quantity)
    if quantity <= ZERO:
        raise WorkOrderError(INVALID_QUANTITY, "Issue quantity must be greater than zero.")
    if quantity > wom.remaining_quantity:
        raise WorkOrderError(OVER_ISSUE, f"Cannot issue more than the {wom.remaining_quantity} remaining for {wom.component.sku}.")

    if location is not None:
        if not _is_nettable(location) or location.site_id != wo.site_id:
            raise WorkOrderError(NON_NETTABLE_LOCATION, "Chosen location is not a nettable stock location at the work order's site.")
        if _location_available(wo.tenant, wom.component, location) < quantity:
            raise WorkOrderError(INSUFFICIENT_STOCK, f"Insufficient stock for {wom.component.sku} at {location.name}.")
        source = location
    else:
        source = _pick_source_location(wo.tenant, wom.component, wo.site, quantity, prefer=wom.source_location)
        if source is None:
            raise WorkOrderError(INSUFFICIENT_STOCK, f"No nettable location has {quantity} of {wom.component.sku} at {wo.site.name}.")

    movement = apply_movement(
        tenant=wo.tenant, product=wom.component, location=source,
        movement_type="WORK_ORDER_ISSUE", qty_delta=-quantity,
        ref_type=WO_REF_TYPE, ref_id=wo.work_order_number,
        notes=f"Issue to {wo.work_order_number}", lot_code=lot_code,
        serial_number=serial_number, expiry_date=expiry_date, user=user, bin=bin)

    issued_cost = (-(movement.value or ZERO)).quantize(COST_DP, rounding=ROUND_HALF_UP)
    wom.issued_quantity = (wom.issued_quantity or ZERO) + quantity
    wom.issued_cost = (wom.issued_cost or ZERO) + issued_cost
    if wom.source_location_id is None:
        wom.source_location = source
    _update_material_status(wom)
    wom.save(update_fields=["issued_quantity", "issued_cost", "status", "source_location"])

    wo.wip_material_cost = (wo.wip_material_cost or ZERO) + issued_cost
    wo.save(update_fields=["wip_material_cost"])
    return movement


@transaction.atomic
def issue_all_available_materials(wo, user):
    """Issue each material's remaining requirement, capped at what one nettable
    location can supply. Returns a list of (material, issued_qty) results."""
    if wo.status not in ("RELEASED", "PARTIALLY_COMPLETED"):
        raise WorkOrderError(NOT_RELEASED, "Release the work order before issuing materials.")
    results = []
    for wom in wo.materials.select_related("component", "source_location").all():
        remaining = wom.remaining_quantity
        if remaining <= ZERO:
            continue
        loc, avail = _best_source_location(wo.tenant, wom.component, wo.site, prefer=wom.source_location)
        if loc is None or avail <= ZERO:
            continue
        take = min(remaining, avail)
        issue_material(wom, take, user, location=loc)
        results.append((wom, take))
    return results


# --------------------------------------------------------------------------- #
# Completion
# --------------------------------------------------------------------------- #
def _completion_unit_cost(wo):
    """Value finished goods at the work order's accumulated material cost spread
    over its planned quantity, else the product standard cost."""
    if (wo.wip_material_cost or ZERO) > ZERO and (wo.quantity or ZERO) > ZERO:
        return (wo.wip_material_cost / wo.quantity).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return Decimal(wo.product.standard_cost or ZERO)


@transaction.atomic
def complete_work_order(wo, quantity, user, location=None, bin=None,
                        lot_code=None, serial_number=None, expiry_date=None):
    """Receive ``quantity`` finished goods from the work order into stock via an
    append-only WORK_ORDER_COMPLETION movement. Returns (movement, warning)."""
    if wo.status not in ("RELEASED", "PARTIALLY_COMPLETED"):
        raise WorkOrderError(NOT_RELEASED, "Release the work order before completing finished goods.")
    quantity = Decimal(quantity)
    if quantity <= ZERO:
        raise WorkOrderError(INVALID_QUANTITY, "Completion quantity must be greater than zero.")
    new_completed = (wo.quantity_completed or ZERO) + quantity
    if new_completed > (wo.quantity or ZERO):
        raise WorkOrderError(OVER_COMPLETION, f"Cannot complete more than the planned {wo.quantity}.")

    warning = None
    any_issued = any((m.issued_quantity or ZERO) > ZERO for m in wo.materials.all())
    if not any_issued:
        warning = MATERIALS_NOT_ISSUED  # allowed, but flag it

    dest = _pick_dest_location(wo.tenant, wo.site, prefer=location)
    if dest is None:
        raise WorkOrderError(MISSING_DEFAULT_LOCATION, "No nettable stock location at the work order's site to receive finished goods.")

    unit_cost = _completion_unit_cost(wo)
    movement = apply_movement(
        tenant=wo.tenant, product=wo.product, location=dest,
        movement_type="WORK_ORDER_COMPLETION", qty_delta=quantity,
        ref_type=WO_REF_TYPE, ref_id=wo.work_order_number,
        notes=f"Completion of {wo.work_order_number}", unit_cost=unit_cost,
        lot_code=lot_code, serial_number=serial_number, expiry_date=expiry_date, user=user, bin=bin)

    fg_value = (unit_cost * quantity).quantize(COST_DP, rounding=ROUND_HALF_UP)
    wo.quantity_completed = new_completed
    wo.finished_goods_cost = (wo.finished_goods_cost or ZERO) + fg_value
    wo.actual_end_date = timezone.localdate()
    if wo.quantity_completed >= wo.quantity:
        wo.status = "COMPLETED"
        wo.completed_at = timezone.now()
    else:
        wo.status = "PARTIALLY_COMPLETED"
    wo.save(update_fields=["quantity_completed", "finished_goods_cost", "actual_end_date",
                           "status", "completed_at"])
    return movement, warning
