"""Full physical stock-take: snapshot, staleness guard, valuation and posting.

A stock-take counts EVERY on-hand line in scope (a whole location or a whole
site) rather than a hand-picked set. It reuses the inventory movement ledger,
the cycle-count staleness guard (`current_on_hand`), lot-aware valuation
(`lot_layer_unit_cost`) and the Inventory vs Inventory-Adjustment GL path
(`post_stock_take_adjustment`). Nothing here duplicates cycle-count logic; the
shared pieces live in `core.services.inventory` / `core.services.gl`.

Design notes:
  * Line granularity follows the most precise balance available: lot/serial
    lines when the product has lot balances, else bin lines when bin balances
    exist, else a single product line at the location.
  * `expected_qty_snapshot` is the book quantity frozen at snapshot time. The
    staleness guard re-reads the live book before posting; if it has drifted the
    line is marked STALE, expected/variance are refreshed, and the session is
    bounced back to REVIEW for re-approval (never silently posted).
  * Variance is posted as an ADJUSTMENT movement so on-hand becomes the counted
    quantity, with positive lot/serial variances valued at the lot's layer cost
    (product average is only the documented fallback for missing layers).
"""
from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from core.models import (
    StockTakeSession, StockTakeLine, Location, Product,
    InventoryBalance, InventoryLotBalance, InventoryBinBalance, InventoryMovement,
)
from core.services.inventory import (
    apply_movement, current_on_hand, lot_layer_unit_cost,
)
from core.services.gl import post_stock_take_adjustment

CENTS = Decimal("0.01")


def scope_locations(session):
    """Locations covered by the session's scope (a single location, or every
    location under the chosen site)."""
    if session.scope == StockTakeSession.Scope.SITE and session.site_id:
        return list(Location.objects.filter(tenant=session.tenant, site=session.site).order_by("name"))
    if session.location_id:
        return [session.location]
    return []


def _expected_unit_cost(tenant, product, location, *, lot_code=None, serial_number=None, expiry_date=None):
    """Cost used to value a line's expected quantity (and a positive variance):
    the lot's remaining layer cost for lot/serial items, else product average."""
    if lot_code or serial_number or expiry_date:
        uc = lot_layer_unit_cost(tenant, product, location,
                                 lot_code=lot_code, serial_number=serial_number, expiry_date=expiry_date)
        if uc is not None:
            return uc
    return product.average_cost or Decimal("0.0000")


def _last_movement_at(tenant, product, location):
    agg = (InventoryMovement.objects
           .filter(tenant=tenant, product=product, location=location)
           .aggregate(m=Max("created_at")))
    return agg["m"]


@transaction.atomic
def generate_snapshot(session, *, user=None):
    """Populate the session with one line per on-hand balance in scope, freezing
    the expected quantity and its value. Idempotent: clears any prior (unposted)
    snapshot lines first. Returns the number of lines created."""
    if session.status not in (StockTakeSession.Status.DRAFT, StockTakeSession.Status.SNAPSHOTTED):
        raise ValueError("Snapshot can only be generated for a draft session.")

    tenant = session.tenant
    session.lines.all().delete()
    locations = scope_locations(session)
    created = 0

    for location in locations:
        # Products with lot/serial balances -> one line per lot/serial.
        lot_rows = (InventoryLotBalance.objects
                    .filter(tenant=tenant, location=location, on_hand__gt=0)
                    .select_related("product"))
        lot_products = set()
        for lb in lot_rows:
            lot_products.add(lb.product_id)
            uc = _expected_unit_cost(tenant, lb.product, location,
                                     lot_code=lb.lot_code, serial_number=lb.serial_number,
                                     expiry_date=lb.expiry_date)
            StockTakeLine.objects.create(
                session=session, product=lb.product, location=location,
                lot_code=lb.lot_code, serial_number=lb.serial_number, expiry_date=lb.expiry_date,
                uom=lb.product.base_uom,
                expected_qty_snapshot=lb.on_hand,
                expected_unit_cost=uc,
                expected_value_snapshot=(lb.on_hand * uc).quantize(CENTS, rounding=ROUND_HALF_UP),
                last_movement_at=_last_movement_at(tenant, lb.product, location),
            )
            created += 1

        # Products with bin balances but no lot tracking -> one line per bin.
        bin_rows = (InventoryBinBalance.objects
                    .filter(tenant=tenant, location=location, on_hand__gt=0)
                    .exclude(product_id__in=lot_products)
                    .select_related("product", "bin"))
        bin_products = set()
        for bb in bin_rows:
            bin_products.add(bb.product_id)
            uc = _expected_unit_cost(tenant, bb.product, location)
            StockTakeLine.objects.create(
                session=session, product=bb.product, location=location, bin=bb.bin,
                uom=bb.product.base_uom,
                expected_qty_snapshot=bb.on_hand,
                expected_unit_cost=uc,
                expected_value_snapshot=(bb.on_hand * uc).quantize(CENTS, rounding=ROUND_HALF_UP),
                last_movement_at=_last_movement_at(tenant, bb.product, location),
            )
            created += 1

        # Everything else -> one product line at the location total.
        bal_rows = (InventoryBalance.objects
                    .filter(tenant=tenant, location=location, on_hand__gt=0)
                    .exclude(product_id__in=lot_products | bin_products)
                    .select_related("product"))
        for bal in bal_rows:
            uc = _expected_unit_cost(tenant, bal.product, location)
            StockTakeLine.objects.create(
                session=session, product=bal.product, location=location,
                uom=bal.product.base_uom,
                expected_qty_snapshot=bal.on_hand,
                expected_unit_cost=uc,
                expected_value_snapshot=(bal.on_hand * uc).quantize(CENTS, rounding=ROUND_HALF_UP),
                last_movement_at=_last_movement_at(tenant, bal.product, location),
            )
            created += 1

    session.status = StockTakeSession.Status.SNAPSHOTTED
    session.snapshot_at = timezone.now()
    if user is not None and session.started_by_id is None:
        session.started_by = user
    session.save(update_fields=["status", "snapshot_at", "started_by"])
    return created


def recompute_variance(line):
    """Recompute a line's variance qty/value from its counted vs expected qty.
    A positive (found) variance is valued at the line's expected unit cost; a
    negative variance is valued the same way for the GL preview (the posted
    movement value is authoritative)."""
    if line.counted_qty is None:
        line.variance_qty = Decimal("0.00")
        line.variance_value = Decimal("0.00")
        return
    var = Decimal(line.counted_qty) - Decimal(line.expected_qty_snapshot)
    line.variance_qty = var
    uc = line.expected_unit_cost or Decimal("0.0000")
    line.variance_value = (var * uc).quantize(CENTS, rounding=ROUND_HALF_UP)


def refresh_staleness(session):
    """Re-read the live book for each line; where it has drifted from the frozen
    expected quantity, refresh expected/variance and mark the line STALE. Returns
    True if any line is stale. Mirrors the cycle-count staleness guard."""
    tenant = session.tenant
    any_stale = False
    for line in session.lines.select_related("product", "location", "bin").all():
        live = current_on_hand(tenant, line.product, line.location,
                               lot_code=line.lot_code, serial_number=line.serial_number,
                               expiry_date=line.expiry_date, bin=line.bin)
        if live != line.expected_qty_snapshot:
            any_stale = True
            line.expected_qty_snapshot = live
            line.expected_unit_cost = _expected_unit_cost(
                tenant, line.product, line.location,
                lot_code=line.lot_code, serial_number=line.serial_number, expiry_date=line.expiry_date)
            line.expected_value_snapshot = (live * line.expected_unit_cost).quantize(CENTS, rounding=ROUND_HALF_UP)
            line.last_movement_at = _last_movement_at(tenant, line.product, line.location)
            recompute_variance(line)
            if line.count_status != StockTakeLine.CountStatus.POSTED:
                line.count_status = StockTakeLine.CountStatus.STALE
            line.save()
    return any_stale


@transaction.atomic
def post_session(session, *, user=None, lock_date=None):
    """Post every line's variance as an ADJUSTMENT movement and book the net GL
    impact. Returns (ok, reason). Refuses to post unless the session is APPROVED;
    runs the staleness guard first and, if anything drifted, refreshes and bounces
    the session back to REVIEW without posting. Idempotent: a POSTED session is a
    no-op. `lock_date` shifts the GL entry date into the current open period when
    the count date falls in a closed period."""
    if session.status == StockTakeSession.Status.POSTED:
        return True, "already_posted"
    if session.status != StockTakeSession.Status.APPROVED:
        return False, "not_approved"

    tenant = session.tenant
    if refresh_staleness(session):
        session.status = StockTakeSession.Status.REVIEW
        session.approved_by = None
        session.save(update_fields=["status", "approved_by"])
        return False, "stale"

    # Closed-period rule: never post into a locked period. If the count date is
    # on/before the lock date, book the GL into the current open period instead.
    entry_date = session.count_date
    if lock_date is not None and entry_date <= lock_date:
        entry_date = timezone.localdate()

    net_value = Decimal("0.00")
    for line in session.lines.select_related("product", "location", "bin").all():
        var = Decimal(line.variance_qty)
        if var == Decimal("0.00"):
            line.count_status = StockTakeLine.CountStatus.POSTED
            line.save(update_fields=["count_status"])
            continue
        unit_cost = None
        # A positive (found) variance on a lot/serial item is valued at that
        # lot's existing layer cost; a negative variance consumes the lot's own
        # layers (lot-scoped FIFO) so its movement value is already the lot cost.
        if var > 0 and (line.lot_code or line.serial_number or line.expiry_date):
            unit_cost = lot_layer_unit_cost(
                tenant, line.product, line.location,
                lot_code=line.lot_code, serial_number=line.serial_number, expiry_date=line.expiry_date)
        movement = apply_movement(
            tenant=tenant, product=line.product, location=line.location,
            movement_type="ADJUSTMENT", qty_delta=var,
            ref_type="STOCK_TAKE", ref_id=str(session.id),
            notes=f"Stock-take {session.reference or session.id} variance",
            lot_code=line.lot_code, serial_number=line.serial_number, expiry_date=line.expiry_date,
            unit_cost=unit_cost, user=user, bin=line.bin,
        )
        net_value += movement.value or Decimal("0.00")
        line.variance_value = movement.value or Decimal("0.00")
        line.count_status = StockTakeLine.CountStatus.POSTED
        line.save(update_fields=["count_status", "variance_value"])

    post_stock_take_adjustment(tenant, session, net_value, user=user, entry_date=entry_date)

    session.status = StockTakeSession.Status.POSTED
    session.posted_by = user
    session.posted_at = timezone.now()
    session.save(update_fields=["status", "posted_by", "posted_at"])
    return True, "posted"
