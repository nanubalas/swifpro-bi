from decimal import Decimal
from django.db import transaction
from core.models import InventoryBalance, InventoryLotBalance, InventoryMovement, InventoryReservation

@transaction.atomic
def apply_movement(*, tenant, product, location, movement_type, qty_delta, ref_type, ref_id, notes=None, lot_code=None, serial_number=None, expiry_date=None):
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

    InventoryMovement.objects.create(
        tenant=tenant,
        product=product,
        location=location,
        movement_type=movement_type,
        qty_delta=qty_delta,
        ref_type=ref_type,
        ref_id=str(ref_id),
        notes=notes or "",
        lot_code=lot_code,
        serial_number=serial_number,
        expiry_date=expiry_date,
    )
    return bal


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
