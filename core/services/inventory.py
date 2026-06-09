from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from django.db.models import Sum, F
from core.models import (
    InventoryBalance, InventoryLotBalance, InventoryMovement, InventoryReservation,
    InventoryCostLayer, InventoryIssueCost, Product,
)

CENTS = Decimal("0.01")
COST_DP = Decimal("0.0001")


def _total_on_hand(tenant, product):
    agg = InventoryBalance.objects.filter(tenant=tenant, product=product).aggregate(s=Sum("on_hand"))
    return agg["s"] or Decimal("0.00")


def _consume_fifo_layers(tenant, product, qty, fallback_cost, location,
                         lot_code=None, serial_number=None, expiry_date=None):
    """Consume `qty` from the oldest FIFO layers *at this location*; return
    (total_cost, consumed) where consumed is a list of (layer, qty_taken,
    unit_cost) entries (layer is None for any uncovered shortfall).

    Layers are scoped per location so an outbound at one warehouse never
    relieves another warehouse's stock (C5). When a lot/serial/expiry is given,
    only that lot's layers are consumed, so the issue is costed from the lot it
    actually issued rather than the global FIFO queue (M6). If layers run dry
    (negative stock allowed), the shortfall is valued at `fallback_cost`."""
    remaining = qty
    cost = Decimal("0.00")
    consumed = []
    qs = (InventoryCostLayer.objects
          .select_for_update()
          .filter(tenant=tenant, product=product, location=location, qty_remaining__gt=0))
    if lot_code or serial_number or expiry_date:
        qs = qs.filter(lot_code=lot_code, serial_number=serial_number, expiry_date=expiry_date)
    for layer in qs.order_by("received_at", "id"):
        if remaining <= 0:
            break
        take = min(remaining, layer.qty_remaining)
        cost += take * layer.unit_cost
        layer.qty_remaining -= take
        layer.save(update_fields=["qty_remaining"])
        consumed.append((layer, take, layer.unit_cost))
        remaining -= take
    if remaining > 0:
        fb = fallback_cost or Decimal("0.0000")
        cost += remaining * fb
        consumed.append((None, remaining, fb))
    return cost, consumed


def select_fefo_lots(*, tenant, product, location, qty):
    """Pick lot balances to issue earliest-expiry-first (then oldest), returning
    [(InventoryLotBalance, qty_to_take)] until `qty` is satisfied or stock runs
    out. Lots without an expiry sort last. Identifies the actual lot to issue
    under FEFO; callers issue those lots so each is costed from its own cost
    layers (M6). Does not move stock itself."""
    remaining = Decimal(qty)
    picks = []
    if remaining <= 0:
        return picks
    lots = (InventoryLotBalance.objects
            .filter(tenant=tenant, product=product, location=location, on_hand__gt=0)
            .order_by(F("expiry_date").asc(nulls_last=True), "id"))
    for lb in lots:
        if remaining <= 0:
            break
        take = min(remaining, lb.on_hand)
        picks.append((lb, take))
        remaining -= take
    return picks


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
    # Lock the product row FIRST so concurrent movements for the same product
    # serialise through the valuation maths. Without this, two simultaneous
    # receipts both read the same prior on-hand/average and the last writer
    # clobbers the other's moving-average (corrupted COGS/valuation).
    product = Product.objects.select_for_update().get(pk=product.pk)

    # Quantity on hand BEFORE this movement (company-wide, for the average).
    # Read AFTER the product lock so it reflects any just-committed movement.
    prior_qty = _total_on_hand(tenant, product)

    bal, _ = InventoryBalance.objects.select_for_update().get_or_create(
        tenant=tenant, product=product, location=location,
        defaults={"on_hand": Decimal("0.00"), "reserved": Decimal("0.00"), "site_id": location.site_id}
    )
    new_on_hand = (bal.on_hand or Decimal("0.00")) + qty_delta
    # Optionally refuse to drive stock negative (H7). Opt-in per tenant; the
    # raise rolls back the surrounding transaction so no movement is recorded.
    if qty_delta < 0 and new_on_hand < 0 and getattr(tenant, "block_negative_stock", False):
        from django.core.exceptions import ValidationError
        raise ValidationError(
            f"Insufficient stock for {product.sku} at {location.name}: "
            f"on hand {bal.on_hand or Decimal('0.00')}, requested {-qty_delta}."
        )
    bal.on_hand = new_on_hand
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
        new_lot_on_hand = (lot_bal.on_hand or Decimal("0.00")) + qty_delta
        # Under strict control, refuse to issue more of a lot/serial than is on
        # hand. For serials (on_hand is 0/1) this enforces the 1-on-hand rule:
        # a serial that isn't in stock can't be issued (M6).
        if qty_delta < 0 and new_lot_on_hand < 0 and getattr(tenant, "block_negative_stock", False):
            from django.core.exceptions import ValidationError
            label = serial_number or lot_code or (expiry_date and str(expiry_date)) or "lot"
            raise ValidationError(
                f"Insufficient stock for {product.sku} ({label}) at {location.name}: "
                f"on hand {lot_bal.on_hand or Decimal('0.00')}, requested {-qty_delta}."
            )
        lot_bal.on_hand = new_lot_on_hand
        lot_bal.save()

    # ----- Valuation -----
    prior_avg = product.average_cost or Decimal("0.0000")
    is_fifo = product.cost_method == Product.CostMethod.FIFO
    is_standard = product.cost_method == Product.CostMethod.STANDARD
    consumed = []  # FIFO layers relieved by an outbound (for the issue-cost trail)

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
            # FIFO products also get a cost layer, tagged with the lot it was
            # received under so a later issue of that lot is costed from it (M6).
            if is_fifo:
                InventoryCostLayer.objects.create(
                    tenant=tenant, product=product, location=location,
                    lot_code=lot_code, serial_number=serial_number, expiry_date=expiry_date,
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
            # Consume the issued lot's layers (specific identification) when a lot
            # is given, else the global FIFO queue. `consumed` feeds the issue-cost
            # audit trail below.
            cost, consumed = _consume_fifo_layers(
                tenant, product, out_qty, prior_avg, location,
                lot_code=lot_code, serial_number=serial_number, expiry_date=expiry_date)
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

    # Record the issue-cost trail: which layer(s) this outbound consumed, the
    # quantity costed and the resulting cost, so COGS is traceable to the exact
    # lot/layer issued (M6).
    for layer, take, layer_unit_cost in consumed:
        InventoryIssueCost.objects.create(
            tenant=tenant, movement=movement, cost_layer=layer,
            lot_code=lot_code, serial_number=serial_number, expiry_date=expiry_date,
            qty=take, unit_cost=layer_unit_cost,
            total_cost=(take * layer_unit_cost).quantize(CENTS, rounding=ROUND_HALF_UP),
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
    # Available-to-promise: refuse to reserve more than is unreserved on hand
    # when the tenant runs strict stock control. The lock above makes the
    # check race-free. Off by default, so over-reservation only warns (M7).
    if getattr(tenant, "block_negative_stock", False):
        available = (bal.on_hand or Decimal("0.00")) - (bal.reserved or Decimal("0.00"))
        if qty > available:
            from django.core.exceptions import ValidationError
            raise ValidationError(
                f"Cannot reserve {qty} of {product.sku} at {location.name}: "
                f"only {available} available to promise."
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

def _unhold(tenant, reservation, amount):
    """Decrement the held `reserved` quantity on the balance (and lot balance)
    for a reservation by `amount`. Resilient to a missing balance/lot row so a
    release/consume never gets stuck (M14)."""
    bal = (InventoryBalance.objects.select_for_update()
           .filter(tenant=tenant, product=reservation.product, location=reservation.location).first())
    if bal is not None:
        bal.reserved = (bal.reserved or Decimal("0.00")) - amount
        bal.save()
    if reservation.lot_code or reservation.serial_number or reservation.expiry_date:
        lot_bal = (InventoryLotBalance.objects.select_for_update()
                   .filter(tenant=tenant, product=reservation.product, location=reservation.location,
                           lot_code=reservation.lot_code, serial_number=reservation.serial_number,
                           expiry_date=reservation.expiry_date).first())
        if lot_bal is not None:
            lot_bal.reserved = (lot_bal.reserved or Decimal("0.00")) - amount
            lot_bal.save()


@transaction.atomic
def release_reservations(*, tenant, ref_type, ref_id):
    """Release all active reservations for a given ref (e.g. order cancelled)."""
    qs = InventoryReservation.objects.select_for_update().filter(
        tenant=tenant, ref_type=ref_type, ref_id=str(ref_id), status=InventoryReservation.Status.ACTIVE
    )
    for r in qs:
        _unhold(tenant, r, r.qty)
        r.status = InventoryReservation.Status.RELEASED
        r.save(update_fields=["status"])


@transaction.atomic
def consume_reservations(*, tenant, ref_type, ref_id, qty=None):
    """Transition ACTIVE reservations for a ref to CONSUMED as the order is
    fulfilled, releasing the held `reserved` quantity.

    With ``qty=None`` every active reservation for the ref is consumed in full.
    With a quantity, consume up to that amount (oldest first); a reservation
    that is only partially fulfilled is split — the consumed part becomes a
    CONSUMED row and the remainder stays ACTIVE. Returns the qty consumed.

    Distinct from release_reservations (cancellation): both free the reserved
    qty so ATP is correct, but CONSUMED records that the stock actually shipped,
    whereas RELEASED records that it was freed without fulfilment (M-reservation)."""
    remaining = None if qty is None else Decimal(qty)
    consumed_total = Decimal("0.00")
    qs = (InventoryReservation.objects.select_for_update()
          .filter(tenant=tenant, ref_type=ref_type, ref_id=str(ref_id),
                  status=InventoryReservation.Status.ACTIVE)
          .order_by("id"))
    for r in qs:
        if remaining is not None and remaining <= 0:
            break
        take = r.qty if remaining is None else min(remaining, r.qty)
        if take <= 0:
            continue
        _unhold(tenant, r, take)
        if take >= r.qty:
            r.status = InventoryReservation.Status.CONSUMED
            r.save(update_fields=["status"])
        else:
            # Partial fulfilment: shrink the active hold, record a CONSUMED row.
            r.qty = r.qty - take
            r.save(update_fields=["qty"])
            InventoryReservation.objects.create(
                tenant=tenant, product=r.product, location=r.location, qty=take,
                status=InventoryReservation.Status.CONSUMED,
                lot_code=r.lot_code, serial_number=r.serial_number, expiry_date=r.expiry_date,
                ref_type=ref_type, ref_id=str(ref_id))
        consumed_total += take
        if remaining is not None:
            remaining -= take
    return consumed_total


def expire_stale_reservations(*, tenant, older_than):
    """Release ACTIVE reservations created on/before `older_than` (a datetime).

    Stale holds (orders abandoned without fulfilment or cancellation) otherwise
    keep stock reserved forever and understate ATP. Returns the count released."""
    stale = (InventoryReservation.objects
             .filter(tenant=tenant, status=InventoryReservation.Status.ACTIVE,
                     created_at__lte=older_than)
             .values_list("ref_type", "ref_id").distinct())
    n = 0
    for ref_type, ref_id in stale:
        before = (InventoryReservation.objects
                  .filter(tenant=tenant, ref_type=ref_type, ref_id=ref_id,
                          status=InventoryReservation.Status.ACTIVE).count())
        release_reservations(tenant=tenant, ref_type=ref_type, ref_id=ref_id)
        n += before
    return n


def lot_layer_value(tenant, product, location, lot_code=None, serial_number=None, expiry_date=None):
    """Remaining FIFO cost-layer value for a specific lot/serial at a location
    (sum of qty_remaining x unit_cost). Returns None when the lot has no layers
    (e.g. non-FIFO product), so callers can fall back to a documented cost."""
    qs = InventoryCostLayer.objects.filter(
        tenant=tenant, product=product, location=location, qty_remaining__gt=0)
    if lot_code or serial_number or expiry_date:
        qs = qs.filter(lot_code=lot_code, serial_number=serial_number, expiry_date=expiry_date)
    agg = qs.aggregate(v=Sum(F("qty_remaining") * F("unit_cost")), q=Sum("qty_remaining"))
    if not agg["q"]:
        return None
    return agg["v"] or Decimal("0.00"), agg["q"]


def lot_layer_unit_cost(tenant, product, location, lot_code=None, serial_number=None, expiry_date=None):
    """Weighted-average remaining unit cost of a lot's FIFO layers, or None when
    the lot has no remaining layers."""
    res = lot_layer_value(tenant, product, location, lot_code, serial_number, expiry_date)
    if res is None:
        return None
    value, qty = res
    if not qty:
        return None
    return (value / qty).quantize(COST_DP, rounding=ROUND_HALF_UP)
