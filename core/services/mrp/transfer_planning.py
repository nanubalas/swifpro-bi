"""Transfer (inter-site) planning for MRP (Phase 4).

When the engine creates a TRANSFER planned order at a destination site it calls
``plan_transfer``, which:

- validates the source site (present, not the destination, active, same tenant);
- records TRANSFER_REQUEST demand at the source site (needed by the transfer's
  planned release date) and pegs the transfer order to it;
- checks nettable available stock at the source (minus stock already committed
  to other open outbound transfers); and
- if the source is short, raises SOURCE_SITE_SHORTAGE and plans the shortfall at
  the source using its own ItemSitePlanning (BUY -> buy order, MAKE -> make
  order + BOM explosion, TRANSFER -> recursive transfer planning with loop and
  depth guards).

Reuses existing Transfer/inventory models, the inventory snapshot, and the
engine's planned-order/exception helpers. No transfer is created or posted; no
inventory/GL movement happens. Never crashes on bad data.
"""
from decimal import Decimal

from core.services.mrp import exceptions as mrp_exc
from core.services.mrp import inventory_snapshot, lot_sizing

ZERO = Decimal("0.00")

# Recursion ceiling for chained transfers (overridable in tests).
MAX_TRANSFER_DEPTH = 10

OPEN_TRANSFER_STATUSES = {"DRAFT", "DISPATCHED"}


def open_outbound_qty(tenant, product, source_site):
    """Quantity of ``product`` at ``source_site`` already committed to leave on
    open outbound transfers but not yet dispatched (dispatched stock has already
    left on-hand, so it is not subtracted again)."""
    from core.models import InventoryTransferLine
    total = ZERO
    lines = (InventoryTransferLine.objects
             .filter(transfer__tenant=tenant, transfer__from_location__site=source_site,
                     transfer__status__in=OPEN_TRANSFER_STATUSES, product=product)
             .select_related("transfer"))
    for line in lines:
        on_order = (line.qty or ZERO) - (line.dispatched_qty or ZERO)
        if on_order > ZERO:
            total += on_order
    return total


def source_available(run, product, source_site):
    """Nettable on-hand at the source site, minus stock already committed to
    other open outbound transfers. Excludes quarantine/damaged/transit/returns,
    reserved and expired stock (via inventory_snapshot)."""
    available, _excluded, _expired = inventory_snapshot.nettable_on_hand(
        run.tenant, product, source_site, as_of=run.planning_start_date)
    available = available - open_outbound_qty(run.tenant, product, source_site)
    return available if available > ZERO else ZERO


def take_source_availability(run, product, source_site, needed):
    """Consume from the source site's available stock (cached per run) so several
    transfers pulling from the same source share it."""
    key = (product.id, source_site.id)
    cache = run._transfer_src_avail
    if key not in cache:
        cache[key] = source_available(run, product, source_site)
    avail = cache[key]
    if avail <= ZERO:
        return ZERO
    take = min(avail, needed)
    cache[key] = avail - take
    return take


def plan_transfer(run, dest_profile, transfer_po, depth, ancestors_sites):
    """Plan the source side of a TRANSFER planned order: validate, raise source
    demand, check availability, and plan any shortfall at the source site."""
    from core.models import MRPDemand, MRPPegging

    product = dest_profile.product
    dest_site = dest_profile.site
    source_site = dest_profile.default_transfer_from_site

    # --- Source-site validation ---
    if source_site is None:
        mrp_exc.raise_exception(
            run, "MISSING_TRANSFER_SOURCE_SITE",
            f"No transfer source site for {product.sku} at {dest_site.name}.",
            product=product, site=dest_site, planned_order=transfer_po)
        mrp_exc.bump_level(transfer_po, "WARNING")
        return
    if source_site.id == dest_site.id:
        mrp_exc.raise_exception(
            run, "INVALID_TRANSFER_SOURCE_SITE",
            f"Transfer source site equals destination ({dest_site.name}) for {product.sku}.",
            product=product, site=dest_site, planned_order=transfer_po)
        mrp_exc.bump_level(transfer_po, "WARNING")
        return
    if source_site.tenant_id != run.tenant_id or not source_site.is_active:
        mrp_exc.raise_exception(
            run, "INVALID_TRANSFER_SOURCE_SITE",
            f"Transfer source site {source_site.name} for {product.sku} is inactive or inaccessible.",
            product=product, site=dest_site, planned_order=transfer_po)
        mrp_exc.bump_level(transfer_po, "WARNING")
        return

    # --- Recursion guards ---
    if depth >= MAX_TRANSFER_DEPTH:
        mrp_exc.raise_exception(
            run, "TRANSFER_MAX_DEPTH_EXCEEDED",
            f"Transfer sourcing for {product.sku} exceeded max depth ({MAX_TRANSFER_DEPTH}); stopped.",
            product=product, site=source_site)
        return
    if source_site.id in ancestors_sites:
        mrp_exc.raise_exception(
            run, "TRANSFER_LOOP_DETECTED",
            f"Transfer loop for {product.sku} via {source_site.name}; stopped to avoid a cycle.",
            product=product, site=source_site)
        return

    required_date = transfer_po.planned_release_date or transfer_po.required_date
    qty = transfer_po.quantity or ZERO

    # Source-site demand to fulfil the transfer.
    demand = MRPDemand.objects.create(
        mrp_run=run, tenant=run.tenant, product=product, site=source_site,
        demand_type="TRANSFER_REQUEST",
        source_document_type="MRPPlannedOrder", source_document_id=transfer_po.planned_order_number,
        source_line_id="", required_date=required_date,
        quantity=qty, open_quantity=qty, priority=0)
    # Transfer order -> source demand (traceability).
    MRPPegging.objects.create(
        tenant=run.tenant, planned_order=transfer_po, demand=demand,
        pegged_quantity=qty, required_date=required_date,
        supply_date=transfer_po.planned_receipt_date, shortage_quantity=ZERO)

    available = take_source_availability(run, product, source_site, qty)
    net = qty - available
    if net <= ZERO:
        return  # source can cover the transfer from stock

    mrp_exc.raise_exception(
        run, "SOURCE_SITE_SHORTAGE",
        f"Source site {source_site.name} is short {net} of {product.sku} for transfer to {dest_site.name}.",
        product=product, site=source_site, planned_order=transfer_po)

    _plan_source(run, product, source_site, demand, net, required_date, transfer_po,
                 depth, ancestors_sites)


def _plan_source(run, product, source_site, demand, net, required_date, transfer_po,
                 depth, ancestors_sites):
    from core.models import ItemSitePlanning, MRPPegging
    from core.services.mrp import engine, bom_explosion

    profile = (ItemSitePlanning.objects
               .filter(tenant=run.tenant, product=product, site=source_site, is_active=True)
               .select_related("product", "site", "default_supplier").first())
    if profile is None or not profile.mrp_enabled:
        mrp_exc.raise_exception(
            run, "MISSING_COMPONENT_PLANNING",
            f"No active MRP planning profile for {product.sku} at source site {source_site.name}.",
            product=product, site=source_site)
        return

    qty, capped, notes = lot_sizing.size_order(profile, net)
    if qty <= ZERO:
        return

    po = engine.create_planned_order(
        run, profile, qty, required_date, profile.lead_time_days or 0, profile.source_type,
        parent_po=transfer_po,
        transfer_from_site=(profile.default_transfer_from_site if profile.source_type == "TRANSFER" else None))

    MRPPegging.objects.create(
        tenant=run.tenant, planned_order=po, demand=demand,
        pegged_quantity=min(qty, net), required_date=required_date,
        supply_date=po.planned_receipt_date, shortage_quantity=ZERO)

    for code, msg in notes:
        mrp_exc.raise_exception(run, code, msg, product=product, site=source_site,
                                planned_order=po, dedupe_key=(code, profile.id))
        mrp_exc.bump_level(po, "INFO")
    engine.order_exceptions(run, profile, po, run.planning_start_date)

    if capped:
        mrp_exc.raise_exception(
            run, "SHORTAGE",
            f"{product.sku} at {source_site.name}: max order qty too small to cover the transfer.",
            product=product, site=source_site, planned_order=po)

    if profile.source_type == "MAKE":
        bom_explosion.explode_and_plan(
            run, product, source_site, po.quantity, po.planned_release_date, po,
            depth=0, ancestors=frozenset({product.id}))
    elif profile.source_type == "TRANSFER":
        plan_transfer(run, profile, po, depth + 1, ancestors_sites | {source_site.id})
