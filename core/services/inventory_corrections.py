"""Catch-up reconciliation for historical cycle-count valuation drift.

Cycle-count variance movements posted before lot-scoped costing/GL were in place
may have been valued at the product moving-average and/or never posted to the GL
at all. This module finds those movements and, in apply mode, posts *new*
auditable revaluation entries so the inventory subledger and GL reflect the
lot/serial-specific cost - it never deletes or rewrites the original movements.

Control characteristics:
  * dry-run by default; corrections only post when explicitly applied;
  * idempotent - one CycleCountValuationCorrection per original movement, so a
    re-run never double-corrects;
  * closed-period aware - this codebase has no period-close model, so the close
    boundary is supplied as `lock_date`; corrections for movements on/before it
    post into the current open period (or are blocked) with a clear reference to
    the original movement.

Documented caveats (kept consistent with the reconciliation report):
  * Tenant-level reconciliation is the authoritative control figure.
  * Lot fallback to product average is acceptable ONLY when a lot/serial has no
    valid remaining cost layer.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from core.models import (
    InventoryMovement, JournalEntry, JournalLine, CycleCountValuationCorrection,
)

CENTS = Decimal("0.01")

# Movements created by this catch-up (excluded from candidate scanning).
REVAL_REF_TYPE = "CC_REVAL"


def _expected_value(movement):
    """(expected_value, source) for a cycle-count movement, valued at the
    lot/serial cost layer when one exists, else the product-average fallback."""
    from core.services.inventory import lot_layer_unit_cost
    has_lot = bool(movement.lot_code or movement.serial_number or movement.expiry_date)
    unit = None
    if has_lot:
        unit = lot_layer_unit_cost(
            movement.tenant, movement.product, movement.location,
            lot_code=movement.lot_code, serial_number=movement.serial_number,
            expiry_date=movement.expiry_date)
    if unit is not None:
        source = CycleCountValuationCorrection.Source.LOT_LAYER
    else:
        source = CycleCountValuationCorrection.Source.PRODUCT_AVERAGE
        unit = movement.product.average_cost or movement.product.standard_cost or Decimal("0.0000")
    expected = (movement.qty_delta * unit).quantize(CENTS, rounding=ROUND_HALF_UP)
    return expected, source


def _cycle_count_has_gl(tenant, ref_id):
    return JournalEntry.objects.filter(tenant=tenant, ref_type="CYCLE_COUNT", ref_id=str(ref_id)).exists()


def find_drift(tenant, tolerance=Decimal("0.01")):
    """Return a list of report rows for historical cycle-count movements whose
    inventory valuation/GL is not lot-correct and not yet corrected.

    Each row has: movement, tenant/product/location, lot/serial, movement_date,
    qty, original_value, expected_value, variance (expected - original),
    valuation_source, has_gl, gl_impact (inventory GL catch-up needed),
    subledger_reval (subledger revaluation needed), and a suggested action.
    """
    tolerance = Decimal(tolerance)
    rows = []
    corrected = set(CycleCountValuationCorrection.objects
                    .filter(tenant=tenant).values_list("original_movement_id", flat=True))
    movements = (InventoryMovement.objects
                 .filter(tenant=tenant, ref_type="CYCLE_COUNT")
                 .select_related("product", "location").order_by("created_at", "id"))
    for m in movements:
        if m.id in corrected:
            continue  # idempotent: already corrected
        if not (m.lot_code or m.serial_number or m.expiry_date):
            continue  # only lot/serial movements are in scope for lot revaluation
        original = m.value or Decimal("0.00")
        expected, source = _expected_value(m)
        variance = (expected - original).quantize(CENTS)
        has_gl = _cycle_count_has_gl(tenant, m.ref_id)
        # GL catch-up: bring the inventory account to the lot-correct value.
        # If the count never posted GL, the whole expected value is missing;
        # otherwise only the valuation variance needs correcting.
        gl_impact = (expected - (original if has_gl else Decimal("0.00"))).quantize(CENTS)
        subledger_reval = variance  # subledger already holds `original`
        if abs(variance) <= tolerance and abs(gl_impact) <= tolerance:
            continue
        if variance != Decimal("0.00"):
            action = (f"Revalue to {source.label.lower()} and post catch-up GL"
                      if not has_gl else f"Revalue to {source.label.lower()}")
        else:
            action = "Post missing cycle-count GL at lot cost"
        rows.append({
            "tenant": tenant, "movement": m, "movement_id": m.id,
            "product": m.product, "location": m.location,
            "lot_code": m.lot_code, "serial_number": m.serial_number, "expiry_date": m.expiry_date,
            "movement_date": m.created_at.date(), "qty": m.qty_delta,
            "original_value": original, "expected_value": expected, "variance": variance,
            "valuation_source": source, "has_gl": has_gl,
            "gl_impact": gl_impact, "subledger_reval": subledger_reval,
            "journal_entry_ids": list(JournalEntry.objects
                                      .filter(tenant=tenant, ref_type="CYCLE_COUNT", ref_id=str(m.ref_id))
                                      .values_list("id", flat=True)),
            "suggested_action": action,
        })
    return rows


@transaction.atomic
def apply_corrections(tenant, *, tolerance=Decimal("0.01"), lock_date=None,
                      posting_date=None, block_closed=False, user=None):
    """Post auditable revaluation corrections for the drift `find_drift` reports.

    Returns a summary dict: corrected (rows), blocked (closed-period rows when
    block_closed), totals. Idempotent via CycleCountValuationCorrection.
    """
    from core.services.gl import _acc
    posting_date = posting_date or timezone.localdate()
    inv_acc = _acc(tenant, "inventory")
    adj_acc = _acc(tenant, "inventory_adjustment")

    corrected, blocked = [], []
    for row in find_drift(tenant, tolerance=tolerance):
        m = row["movement"]
        in_closed = lock_date is not None and row["movement_date"] <= lock_date
        if in_closed and block_closed:
            blocked.append(row)
            continue

        # Closed period -> post into the current open period, referencing the
        # original. Open period -> post on the original movement's date.
        if in_closed:
            entry_date = posting_date
            post_dt = timezone.now()
            note = (f"Original movement {m.id} dated {row['movement_date']} is in a closed "
                    f"period; correction posted to {entry_date}.")
            posted_current = True
        else:
            entry_date = row["movement_date"]
            post_dt = m.created_at
            note = f"Correction posted in the original period ({entry_date})."
            posted_current = False

        reval_mv = None
        if row["subledger_reval"] != Decimal("0.00"):
            # Pure value adjustment (no quantity change, no layer impact).
            reval_mv = InventoryMovement.objects.create(
                tenant=tenant, site_id=m.site_id, product=m.product, location=m.location,
                movement_type=InventoryMovement.MovementType.ADJUSTMENT,
                qty_delta=Decimal("0.00"), unit_cost=None, value=row["subledger_reval"],
                ref_type=REVAL_REF_TYPE, ref_id=str(m.id),
                notes=f"Cycle-count revaluation of movement {m.id}",
                lot_code=m.lot_code, serial_number=m.serial_number, expiry_date=m.expiry_date,
                created_at=post_dt)

        je = None
        gl_impact = row["gl_impact"]
        if gl_impact != Decimal("0.00"):
            je = JournalEntry.objects.create(
                tenant=tenant, site_id=getattr(m.location, "site_id", None), entry_date=entry_date,
                ref_type=REVAL_REF_TYPE, ref_id=str(m.id),
                memo=f"Cycle-count valuation correction for movement {m.id} ({row['movement_date']})",
                posted_by=user, posted_at=timezone.now())
            amount = abs(gl_impact)
            if gl_impact > Decimal("0.00"):  # inventory value increases
                JournalLine.objects.create(entry=je, account=inv_acc, description="Inventory revaluation", debit=amount, credit=Decimal("0.00"))
                JournalLine.objects.create(entry=je, account=adj_acc, description="Cycle count revaluation", debit=Decimal("0.00"), credit=amount)
            else:
                JournalLine.objects.create(entry=je, account=adj_acc, description="Cycle count revaluation", debit=amount, credit=Decimal("0.00"))
                JournalLine.objects.create(entry=je, account=inv_acc, description="Inventory revaluation", debit=Decimal("0.00"), credit=amount)

        CycleCountValuationCorrection.objects.create(
            tenant=tenant, original_movement=m, product=m.product, location=m.location,
            lot_code=m.lot_code, serial_number=m.serial_number, expiry_date=m.expiry_date,
            original_value=row["original_value"], expected_value=row["expected_value"],
            variance=row["variance"], valuation_source=row["valuation_source"],
            correction_journal=je, reval_movement=reval_mv,
            posted_to_current_period=posted_current, note=note, created_by=user)
        corrected.append(row)

    return {
        "corrected": corrected,
        "blocked": blocked,
        "corrected_count": len(corrected),
        "blocked_count": len(blocked),
        "total_variance": sum((r["variance"] for r in corrected), Decimal("0.00")),
        "total_gl_impact": sum((r["gl_impact"] for r in corrected), Decimal("0.00")),
    }
