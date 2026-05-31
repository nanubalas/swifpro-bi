from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from django.db.models import Sum
from core.models import InventoryBalance, InventoryLotBalance, InventoryMovement, InventoryReservation

CENTS = Decimal("0.01")
COST_DP = Decimal("0.0001")


def _total_on_hand(tenant, product):
    agg = InventoryBalance.objects.filter(tenant=tenant, product=product).aggregate(s=Sum("on_hand"))
    return agg["s"] or Decimal("0.00")


@transaction.atomic
def apply_movement(*, tenant, product, location, movement_type, qty_delta, ref_type, ref_id,
                   notes=None, lot_code=None, serial_number=None, expiry_date=None, unit_cost=None):
    """Apply an inventory movement and maintain valuation.

    Inbound (qty_delta > 0) with a unit_cost updates the product's moving
    weighted-average cost. Outbound movements are valued at the current
    average cost. Every movement stores unit_cost + signed value so the GL
    and stock-valuation reports can rely on it.
    """
    # Quantity on hand BEFORE this movement (company-wide, for the average).
    prior_qty = _total_on_hand(tenant, product)

    bal, _ = InventoryBalance.objects.select_for_update().get_or_create(
        tenant=tenant, product=product, location=location,
        defaults={"on_hand": Decimal("0.00"), "reserved": Decimal("0.00")}
    )
    bal.on_hand = (bal.on_hand or Decimal("0.00")) + qty_delta
    bal.save()

    # Lot-level balance (optional)
    if lot_code or serial_number or expiry_date:
        lot_bal, _ = InventoryLotBalance.objects.select_for_update().get_or_create(
            tenant=tenant, product=product, location=location,
            lot_code=lot_code, serial_number=serial_number, expiry_date=expiry_date,
            defaults={"on_hand": Decimal("0.00"), "reserved": Decimal("0.00")}
        )
        lot_bal.on_hand = (lot_bal.on_hand or Decimal("0.00")) + qty_delta
        lot_bal.save()

    # ----- Valuation -----
    prior_avg = product.average_cost or Decimal("0.0000")
    if qty_delta > 0 and unit_cost is not None:
        # Inbound at a known cost -> recompute moving average.
        unit_cost = Decimal(unit_cost)
        new_qty = prior_qty + qty_delta
        if new_qty > 0:
            new_avg = ((prior_qty * prior_avg) + (qty_delta * unit_cost)) / new_qty
            product.average_cost = new_avg.quantize(COST_DP, rounding=ROUND_HALF_UP)
            product.save(update_fields=["average_cost"])
        move_unit_cost = unit_cost
    else:
        # Outbound, or inbound without an explicit cost: value at current average.
        move_unit_cost = prior_avg

    value = (qty_delta * move_unit_cost).quantize(CENTS, rounding=ROUND_HALF_UP)

    movement = InventoryMovement.objects.create(
        tenant=tenant,
        product=product,
        location=location,
        movement_type=movement_type,
        qty_delta=qty_delta,
        unit_cost=move_unit_cost,
        value=value,
        ref_type=ref_type,
        ref_id=str(ref_id),
        notes=notes or "",
        lot_code=lot_code,
        serial_number=serial_number,
        expiry_date=expiry_date,
    )
    return movement


@transaction.atomic
def reserve_stock(*, tenant, product, location, qty, ref_type, ref_id, lot_code=None, serial_number=None, expiry_date=None):
    """Increase reserved qty (creates reservation record)."""
    if qty <= 0:
        return
    bal, _ = InventoryBalance.objects.select_for_update().get_or_create(
        tenant=tenant, product=product, location=location,
        defaults={"on_hand": Decimal("0.00"), "reserved": Decimal("0.00")}
    )
    bal.reserved = bal.reserved + qty
    bal.save()

    if lot_code or serial_number or expiry_date:
        lot_bal, _ = InventoryLotBalance.objects.select_for_update().get_or_create(
            tenant=tenant, product=product, location=location,
            lot_code=lot_code, serial_number=serial_number, expiry_date=expiry_date,
            defaults={"on_hand": Decimal("0.00"), "reserved": Decimal("0.00")}
        )
        lot_bal.reserved = lot_bal.reserved + qty
        lot_bal.save()

    InventoryReservation.objects.create(
        tenant=tenant, product=product, location=location,
        qty=qty, status=InventoryReservation.Status.ACTIVE,
        lot_code=lot_code, serial_number=serial_number, expiry_date=expiry_date,
        ref_type=ref_type, ref_id=ref_id
    )

@transaction.atomic
def release_reservations(*, tenant, ref_type, ref_id):
    """Release all active reservations for a given ref."""
    qs = InventoryReservation.objects.select_for_update().filter(
        tenant=tenant, ref_type=ref_type, ref_id=ref_id, status=InventoryReservation.Status.ACTIVE
    )
    for r in qs:
        bal = InventoryBalance.objects.select_for_update().get(tenant=tenant, product=r.product, location=r.location)
        bal.reserved = bal.reserved - r.qty
        bal.save()

        if r.lot_code or r.serial_number or r.expiry_date:
            lot_bal = InventoryLotBalance.objects.select_for_update().get(
                tenant=tenant, product=r.product, location=r.location,
                lot_code=r.lot_code, serial_number=r.serial_number, expiry_date=r.expiry_date
            )
            lot_bal.reserved = lot_bal.reserved - r.qty
            lot_bal.save()

        r.status = InventoryReservation.Status.RELEASED
        r.save()
