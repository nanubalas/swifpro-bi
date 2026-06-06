from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from django.db.models import Sum
from core.models import (
    InventoryBalance, InventoryLotBalance, InventoryMovement, InventoryReservation,
    InventoryCostLayer, Product,
)

CENTS = Decimal("0.01")
COST_DP = Decimal("0.0001")


def _total_on_hand(tenant, product):
    agg = InventoryBalance.objects.filter(tenant=tenant, product=product).aggregate(s=Sum("on_hand"))
    return agg["s"] or Decimal("0.00")


def _consume_fifo_layers(tenant, product, qty, fallback_cost):
    """Consume `qty` from the product's oldest FIFO layers; return the total
    cost consumed. If layers run dry (negative stock allowed), the shortfall is
    valued at `fallback_cost`."""
    remaining = qty
    cost = Decimal("0.00")
    layers = (InventoryCostLayer.objects
              .select_for_update()
              .filter(tenant=tenant, product=product, qty_remaining__gt=0)
              .order_by("received_at", "id"))
    for layer in layers:
        if remaining <= 0:
            break
        take = min(remaining, layer.qty_remaining)
        cost += take * layer.unit_cost
        layer.qty_remaining -= take
        layer.save(update_fields=["qty_remaining"])
        remaining -= take
    if remaining > 0:
        cost += remaining * (fallback_cost or Decimal("0.0000"))
    return cost


@transaction.atomic
def apply_movement(*, tenant, product, location, movement_type, qty_delta, ref_type, ref_id,
                   notes=None, lot_code=None, serial_number=None, expiry_date=None, unit_cost=None,
                   user=None, bin=None):
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
        defaults={"on_hand": Decimal("0.00"), "reserved": Decimal("0.00"), "site_id": location.site_id}
    )
    bal.on_hand = (bal.on_hand or Decimal("0.00")) + qty_delta
    if bal.site_id is None:
        bal.site_id = location.site_id  # keep stock site in sync with its location
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
    is_fifo = product.cost_method == Product.CostMethod.FIFO
    is_standard = product.cost_method == Product.CostMethod.STANDARD

    if qty_delta > 0:
        if is_standard:
            # Inventory is always carried at standard cost; the actual purchase
            # cost (passed via unit_cost) becomes a variance handled by the GL.
            std = product.standard_cost or Decimal("0.0000")
            if product.average_cost != std:
                product.average_cost = std  # keep display/valuation consistent
                product.save(update_fields=["average_cost"])
            move_unit_cost = std
        else:
            # Inbound cost basis = explicit unit_cost, else current average.
            cost_in = Decimal(unit_cost) if unit_cost is not None else prior_avg
            # Maintain moving average (used for display + AVERAGE method).
            if unit_cost is not None:
                new_qty = prior_qty + qty_delta
                if new_qty > 0:
                    new_avg = ((prior_qty * prior_avg) + (qty_delta * cost_in)) / new_qty
                    product.average_cost = new_avg.quantize(COST_DP, rounding=ROUND_HALF_UP)
                    product.save(update_fields=["average_cost"])
            # FIFO products also get a cost layer.
            if is_fifo:
                InventoryCostLayer.objects.create(
                    tenant=tenant, product=product,
                    qty_received=qty_delta, qty_remaining=qty_delta,
                    unit_cost=cost_in, ref_type=ref_type, ref_id=str(ref_id),
                )
            move_unit_cost = cost_in
        value = (qty_delta * move_unit_cost).quantize(CENTS, rounding=ROUND_HALF_UP)
    else:
        # Outbound.
        out_qty = -qty_delta
        if is_standard:
            move_unit_cost = product.standard_cost or prior_avg
            value = (qty_delta * move_unit_cost).quantize(CENTS, rounding=ROUND_HALF_UP)
        elif is_fifo:
            cost = _consume_fifo_layers(tenant, product, out_qty, prior_avg)
            value = (-cost).quantize(CENTS, rounding=ROUND_HALF_UP)
            move_unit_cost = (cost / out_qty).quantize(COST_DP, rounding=ROUND_HALF_UP) if out_qty else prior_avg
        else:
            move_unit_cost = prior_avg
            value = (qty_delta * move_unit_cost).quantize(CENTS, rounding=ROUND_HALF_UP)

    movement = InventoryMovement.objects.create(
        tenant=tenant,
        site_id=location.site_id,
        product=product,
        location=location,
        bin=bin,
        movement_type=movement_type,
        user=user,
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
