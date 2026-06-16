"""MRP engine (Phase 2 + 3): time-phased net-requirement planning.

For each MRP-enabled BUY or MAKE ItemSitePlanning profile in scope, the engine:
1. collects demand (sales orders + safety-stock floor) and supply (nettable
   on-hand, open POs, open PRs);
2. walks the demand/supply event dates building projected available balance
   (Projected = Previous + Scheduled Supply - Gross Demand);
3. wherever projected dips below safety stock, sizes a planned order
   (lot sizing -> MOQ -> order multiple -> max cap), offsets the release date
   by lead time, and lifts projected back up;
4. for MAKE planned orders, explodes the parent BOM into dependent component
   demand and recursively plans each component (Phase 3, see bom_explosion);
5. pegs planned orders to the demand they cover; and
6. records exceptions for missing/inferred/invalid data and shortages.

It is idempotent: re-running a run clears that run's prior results first. It
never crashes on bad data - problems become exceptions and planning continues.
No inventory/GL/PO/PR/sales schema or posting logic is touched.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.services.mrp import (
    demand_collector, supply_collector, lot_sizing, lead_time, pegging,
)
from core.services.mrp import exceptions as mrp_exc
from core.services.mrp.numbering import next_planned_order_number

ZERO = Decimal("0.00")
_MAX_ORDERS_PER_BUCKET = 200  # guard against runaway loops when max cap is tiny
_PLANNABLE_SOURCE_TYPES = ["BUY", "MAKE", "TRANSFER"]  # PHANTOM is only planned as a component


def run_mrp(run, user=None):
    """Execute ``run`` and return it with an updated status. Raises only on an
    unexpected, run-fatal error (status then set to CANCELLED)."""
    from core.models import (
        ItemSitePlanning, MRPException, MRPPegging, MRPPlannedOrder, MRPSupply, MRPDemand,
    )

    run.status = "RUNNING"
    run.started_at = timezone.now()
    if user is not None and not run.started_by_id:
        run.started_by = user
    run.save(update_fields=["status", "started_at", "started_by"])

    # Idempotent re-run: clear this run's previous results.
    with transaction.atomic():
        MRPException.objects.filter(mrp_run=run).delete()
        MRPPegging.objects.filter(planned_order__mrp_run=run).delete()
        MRPPlannedOrder.objects.filter(mrp_run=run).delete()
        MRPSupply.objects.filter(mrp_run=run).delete()
        MRPDemand.objects.filter(mrp_run=run).delete()
    run._mrp_exc_seen = set()
    run.__dict__.pop("_forecast_version_resolved", None)  # re-resolve forecast per run
    run._supply_cache = {}      # (product_id, site_id) -> [MRPSupply]; collect once per run
    run._comp_consumed = {}     # (product_id, site_id) -> Decimal consumed by dependent demand
    run._transfer_src_avail = {}  # (product_id, source_site_id) -> Decimal remaining source stock

    profiles = (ItemSitePlanning.objects
                .filter(tenant=run.tenant, is_active=True, mrp_enabled=True,
                        source_type__in=_PLANNABLE_SOURCE_TYPES)
                .select_related("product", "site", "default_supplier"))
    if run.site_scope_id:
        profiles = profiles.filter(site_id=run.site_scope_id)

    try:
        for profile in profiles:
            try:
                with transaction.atomic():
                    _plan_profile(run, profile)
            except Exception as e:  # one bad item must not kill the run
                mrp_exc.raise_exception(
                    run, "SHORTAGE",
                    f"Planning failed for {profile.product.sku} at {profile.site.name}: {e}",
                    product=profile.product, site=profile.site, severity="CRITICAL")

        has_exc = MRPException.objects.filter(mrp_run=run).exists()
        run.status = "COMPLETED_WITH_EXCEPTIONS" if has_exc else "COMPLETED"
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "completed_at"])
    except Exception as e:
        run.status = "CANCELLED"
        run.notes = ((run.notes or "") + f"\nMRP run failed: {e}").strip()
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "notes", "completed_at"])
        raise
    return run


def _plan_profile(run, profile):
    from core.services.mrp import bom_explosion, transfer_planning

    product = profile.product
    site = profile.site
    start = run.planning_start_date

    demands = demand_collector.collect(run, profile)
    supplies = ensure_supply(run, profile)

    safety = profile.safety_stock_qty or ZERO
    if not (run.include_safety_stock and profile.include_safety_stock):
        safety = ZERO

    on_hand = sum((s.available_quantity or ZERO for s in supplies if s.supply_type == "ON_HAND"), ZERO)

    # Scheduled (non on-hand) supply by effective date; clamp past dates to start.
    supply_by_date = {}
    for s in supplies:
        if s.supply_type == "ON_HAND":
            continue
        d = s.receipt_date or start
        if d < start:
            d = start
        supply_by_date[d] = supply_by_date.get(d, ZERO) + (s.available_quantity or ZERO)

    # Gross demand (sales + forecast; safety is a floor, not gross demand) by
    # date. Forecast rows are already net of same-bucket sales consumption, so
    # counting both never double-counts (see forecast_consumption).
    demand_by_date = {}
    for dem in demands:
        if dem.demand_type not in ("SALES_ORDER", "FORECAST"):
            continue
        d = dem.required_date
        if d < start:
            d = start
        demand_by_date[d] = demand_by_date.get(d, ZERO) + (dem.open_quantity or ZERO)

    event_dates = sorted(set([start]) | set(supply_by_date) | set(demand_by_date))

    projected = on_hand
    lead = profile.lead_time_days or 0
    planned_orders = []

    for d in event_dates:
        projected += supply_by_date.get(d, ZERO)
        projected -= demand_by_date.get(d, ZERO)

        guard = 0
        while projected < safety:
            net_req = safety - projected
            qty, capped, notes = lot_sizing.size_order(profile, net_req)
            if qty <= ZERO:
                break
            po = create_planned_order(
                run, profile, qty, d, lead, profile.source_type,
                transfer_from_site=(profile.default_transfer_from_site
                                    if profile.source_type == "TRANSFER" else None))
            planned_orders.append(po)
            for code, msg in notes:
                mrp_exc.raise_exception(run, code, msg, product=product, site=site, planned_order=po,
                                        dedupe_key=(code, profile.id))
                mrp_exc.bump_level(po, "INFO")
            order_exceptions(run, profile, po, start)
            # MAKE orders drive dependent component demand (Phase 3).
            if profile.source_type == "MAKE":
                bom_explosion.explode_and_plan(
                    run, product, site, po.quantity, po.planned_release_date, po,
                    depth=0, ancestors=frozenset({product.id}))
            # TRANSFER orders drive source-site demand and planning (Phase 4).
            elif profile.source_type == "TRANSFER":
                transfer_planning.plan_transfer(
                    run, profile, po, depth=0, ancestors_sites=frozenset({site.id}))
            projected += qty
            guard += 1
            if guard >= _MAX_ORDERS_PER_BUCKET:
                mrp_exc.raise_exception(
                    run, "SHORTAGE",
                    f"{product.sku} at {site.name}: max order qty too small to clear the shortage.",
                    product=product, site=site, planned_order=po)
                break

    uncovered = pegging.peg(run, planned_orders, demands)
    for dem, qty in uncovered:
        if dem.demand_type == "SALES_ORDER":
            mrp_exc.raise_exception(
                run, "SHORTAGE",
                f"{qty} of {product.sku} for sales order {dem.source_document_id} is unmet.",
                product=product, site=site,
                source_document_type=dem.source_document_type,
                source_document_id=dem.source_document_id)

    return planned_orders


def create_planned_order(run, profile, qty, required_date, lead, source_type,
                         parent_po=None, transfer_from_site=None):
    """Create a planned order. A supplier is attached only for BUY orders;
    TRANSFER orders carry the source site; both MAKE and TRANSFER carry the
    parent linkage."""
    from core.models import MRPPlannedOrder
    receipt = required_date
    release = lead_time.release_date(receipt, lead)
    return MRPPlannedOrder.objects.create(
        mrp_run=run, tenant=run.tenant, product=profile.product, site=profile.site,
        source_type=source_type,
        planned_order_number=next_planned_order_number(run.tenant),
        quantity=qty, required_date=required_date,
        planned_receipt_date=receipt, planned_release_date=release,
        supplier=(profile.default_supplier if source_type == "BUY" else None),
        transfer_from_site=transfer_from_site,
        parent_planned_order=parent_po,
        status="SUGGESTED", action_type="CREATE", exception_level="NONE",
        created_by=run.started_by,
    )


def order_exceptions(run, profile, po, start):
    product, site = profile.product, profile.site

    # A supplier is only relevant for BUY orders.
    if po.source_type == "BUY" and profile.default_supplier_id is None:
        mrp_exc.raise_exception(
            run, "MISSING_SUPPLIER",
            f"No default supplier for {product.sku} at {site.name}; planner must choose one.",
            product=product, site=site, planned_order=po, dedupe_key=("supplier", profile.id))
        mrp_exc.bump_level(po, "WARNING")

    if (profile.lead_time_days or 0) == 0:
        mrp_exc.raise_exception(
            run, "MISSING_LEAD_TIME",
            f"No lead time for {product.sku} at {site.name}; release date equals receipt date.",
            product=product, site=site, planned_order=po, dedupe_key=("leadtime", profile.id))
        mrp_exc.bump_level(po, "WARNING")

    if po.planned_release_date and po.planned_release_date < start:
        mrp_exc.raise_exception(
            run, "PAST_DUE_RELEASE",
            f"Planned release {po.planned_release_date} for {product.sku} is already past; expedite.",
            product=product, site=site, planned_order=po)
        mrp_exc.bump_level(po, "WARNING")


def ensure_supply(run, profile):
    """Collect supply for an item-site once per run (cached), so the same item
    reached both as a top-level profile and as a component never produces
    duplicate MRPSupply rows."""
    key = (profile.product_id, profile.site_id)
    cache = getattr(run, "_supply_cache", None)
    if cache is None:
        cache = {}
        run._supply_cache = cache
    if key not in cache:
        cache[key] = supply_collector.collect(run, profile)
    return cache[key]


def take_component_availability(run, profile, needed):
    """Consume from an item's available supply (on-hand + open POs/PRs) to net a
    dependent-demand requirement. Tracks consumption per item-site so multiple
    dependent demands on the same component share the available stock."""
    key = (profile.product_id, profile.site_id)
    supplies = ensure_supply(run, profile)
    total = sum((s.available_quantity or ZERO for s in supplies), ZERO)
    consumed = getattr(run, "_comp_consumed", {}).get(key, ZERO)
    avail = total - consumed
    if avail <= ZERO:
        return ZERO
    take = min(avail, needed)
    run._comp_consumed[key] = consumed + take
    return take
