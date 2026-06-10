"""Customer return (RMA) receipt with per-line disposition.

Returned stock is routed by `ReturnLine.disposition`:

  * RESTOCK            -> sellable inbound at the RMA receive location.
  * QUARANTINE/REPAIR/
    RETURN_TO_SUPPLIER -> inbound to a non-sellable hold (quarantine) location,
                          so it is owned but excluded from availability/ATP.
                          REPAIR and RTS reuse the hold (full repair / supplier-
                          claim workflows are deferred — see the package notes).
  * SCRAP              -> inbound then immediately written off, booking the loss
                          through the existing write-off GL pattern.

Reuses the inventory ledger (serial cardinality enforced there), kit explosion,
and the Inventory-vs-Inventory-Adjustment GL helper. Idempotent: a RECEIVED /
CLOSED RMA is a no-op, and the scrap GL is idempotent on its ref.
"""
from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from core.models import Location, ReturnAuthorization, ReturnLine, InventoryMovement
from core.services.inventory import apply_movement
from core.services.bom import explode_product
from core.services.gl import post_stock_adjustment_value


def hold_location(tenant, *, near=None):
    """A non-sellable quarantine/hold location for `tenant` (preferring one at the
    same site as `near`). Created on demand if none exists, so quarantine works
    out of the box without manual setup."""
    qs = Location.objects.filter(tenant=tenant, type=Location.Type.QUARANTINE)
    if near is not None and getattr(near, "site_id", None):
        loc = qs.filter(site_id=near.site_id).first()
        if loc:
            return loc
    loc = qs.order_by("id").first()
    if loc:
        return loc
    loc, _ = Location.objects.get_or_create(
        tenant=tenant, name="Quarantine",
        defaults={"type": Location.Type.QUARANTINE, "holds_stock": True,
                  "site_id": getattr(near, "site_id", None)})
    return loc


@transaction.atomic
def receive_return(rma, *, user=None):
    """Receive an RMA, routing each line by its disposition. Idempotent."""
    if rma.status in (ReturnAuthorization.Status.RECEIVED, ReturnAuthorization.Status.CLOSED):
        return
    tenant = rma.tenant
    ref_id = f"{rma.channel}:{rma.rma_number}"

    for line in rma.lines.select_related("product").all():
        qty = Decimal(line.qty)
        if qty <= 0:
            continue
        disp = line.disposition or ReturnLine.Disposition.QUARANTINE
        dest = hold_location(tenant, near=rma.receive_location) if line.is_hold else rma.receive_location

        # Returned kits restock as components (mirrors the sales deduction policy).
        for comp, comp_qty in explode_product(line.product, qty):
            apply_movement(
                tenant=tenant, product=comp, location=dest,
                movement_type=InventoryMovement.MovementType.RETURN,
                qty_delta=comp_qty, ref_type="RMA", ref_id=ref_id,
                notes=f"Return received - {line.get_disposition_display()}", user=user,
                lot_code=line.lot_code, serial_number=line.serial_number, expiry_date=line.expiry_date)

            if line.is_scrap:
                # Relieve the just-received unit and book the loss (DR Inventory
                # Adjustments / CR Inventory), idempotent on the scrap ref.
                wo = apply_movement(
                    tenant=tenant, product=comp, location=dest,
                    movement_type=InventoryMovement.MovementType.WRITE_OFF,
                    qty_delta=(comp_qty * Decimal("-1")), ref_type="RMA_SCRAP", ref_id=str(line.id),
                    notes=f"Scrapped on return {rma.rma_number}", user=user,
                    lot_code=line.lot_code, serial_number=line.serial_number, expiry_date=line.expiry_date)
                post_stock_adjustment_value(
                    tenant, wo.value or Decimal("0.00"),
                    ref_type="RMA_SCRAP", ref_id=str(line.id), location=dest,
                    memo=f"Scrap on return {rma.rma_number}", user=user)

        # Stamp inspection on first receipt.
        if user is not None and line.inspected_by_id is None:
            line.inspected_by = user
            line.inspected_at = timezone.now()
            line.save(update_fields=["inspected_by", "inspected_at"])

    rma.status = ReturnAuthorization.Status.RECEIVED
    rma.save(update_fields=["status"])
