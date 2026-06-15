"""BOM explosion and dependent-demand planning for MAKE items (Phase 3).

When the engine creates a MAKE planned order it calls ``explode_and_plan``,
which finds the parent's active BOM, turns each line into WORK_ORDER_COMPONENT
demand (needed by the parent's planned release date), pegs that demand to the
parent order, and recursively plans each component:

- BUY component  -> a BUY planned order (Phase 2 net logic against stock/supply)
- MAKE component -> a MAKE planned order, then its BOM is exploded too
- PHANTOM        -> no planned order; the phantom's BOM is exploded through and
                    its requirement rolls down to the next level

Reuses existing Product/BOM models and the engine's planned-order, supply and
exception helpers. It never crashes on bad data: missing/invalid/circular/too
-deep BOMs and missing component profiles become exceptions and planning
continues. No inventory/GL posting or production execution happens here.
"""
from decimal import Decimal, ROUND_CEILING

from django.core.exceptions import ValidationError

from core.services.mrp import exceptions as mrp_exc
from core.services.mrp import lot_sizing

ZERO = Decimal("0.00")
QTY_DP = Decimal("0.01")

# Recursion ceiling (overridable in tests). Referenced at call time, not as a
# default argument, so a test can lower it and exercise the guard.
MAX_BOM_DEPTH = 20


def find_active_bom(product):
    """Return the active BOM to plan for ``product``: the conventional
    "Default BOM" if present, else the earliest active BOM (matching the order
    used by the existing kit-explosion service). BOMs are not site-scoped in
    this schema, so the same BOM applies at every site."""
    from core.models import BillOfMaterials
    qs = BillOfMaterials.objects.filter(product=product, is_active=True)
    return (qs.filter(name="Default BOM").order_by("created_at", "id").first()
            or qs.order_by("created_at", "id").first())


def component_required_qty(run, line, parent_qty, output_qty, component, site):
    """Component base-unit requirement for ``parent_qty`` of the parent:

        per_unit       = line.qty / output_qty
        required       = parent_qty * per_unit + fixed_qty
        with scrap     = required / (1 - scrap_percent/100)

    then converted from the BOM line UOM to the component base unit. Rounded up
    to avoid planning a fractional shortage. Bad data is reported, not fatal.
    """
    from core.services.uom import to_base_qty

    out = Decimal(output_qty or 1)
    if out <= ZERO:
        out = Decimal(1)
    per_unit = Decimal(line.qty) / out
    req = Decimal(parent_qty) * per_unit
    req += Decimal(getattr(line, "fixed_qty", 0) or 0)

    scrap = Decimal(getattr(line, "scrap_percent", 0) or 0)
    if scrap > ZERO:
        if scrap >= Decimal(100):
            mrp_exc.raise_exception(
                run, "INVALID_BOM",
                f"Scrap percent >= 100 on BOM line for {component.sku}; scrap ignored.",
                product=component, site=site, dedupe_key=("scrap", line.id))
        else:
            req = req / (Decimal(1) - scrap / Decimal(100))

    try:
        req = to_base_qty(component, req, line.uom)
    except ValidationError:
        mrp_exc.raise_exception(
            run, "MISSING_UOM_CONVERSION",
            f"No UOM conversion for component {component.sku}; quantity used as entered.",
            product=component, site=site, dedupe_key=("uom", component.id))

    return Decimal(req).quantize(QTY_DP, rounding=ROUND_CEILING)


def explode_and_plan(run, parent_product, site, parent_qty, components_required_date,
                     parent_po, depth, ancestors, phantom=False):
    """Explode ``parent_product``'s BOM into dependent demand and plan each
    component. ``parent_po`` is the planned order driving the explosion (the
    nearest real MAKE order; for a phantom it is the real order above it)."""
    from core.models import BillOfMaterialsLine, MRPDemand, MRPPegging

    bom = find_active_bom(parent_product)
    if bom is None:
        code = "PHANTOM_BOM_MISSING" if phantom else "MISSING_BOM"
        mrp_exc.raise_exception(
            run, code,
            f"No active BOM for {parent_product.sku}; cannot explode.",
            product=parent_product, site=site,
            planned_order=(None if phantom else parent_po))
        if parent_po is not None and not phantom:
            mrp_exc.bump_level(parent_po, "WARNING")
        return

    lines = list(BillOfMaterialsLine.objects.select_related("component", "uom")
                 .filter(bom=bom).order_by("line_no", "id"))
    if not lines:
        mrp_exc.raise_exception(
            run, "INVALID_BOM",
            f"BOM for {parent_product.sku} has no component lines.",
            product=parent_product, site=site, planned_order=parent_po)
        return

    for line in lines:
        comp = line.component
        req = component_required_qty(run, line, parent_qty, bom.output_qty, comp, site)
        if req <= ZERO:
            continue

        demand = MRPDemand.objects.create(
            mrp_run=run, tenant=run.tenant, product=comp, site=site,
            demand_type="WORK_ORDER_COMPONENT",
            source_document_type="MRPPlannedOrder",
            source_document_id=(parent_po.planned_order_number if parent_po else ""),
            source_line_id=str(line.id),
            required_date=components_required_date, quantity=req, open_quantity=req, priority=0)

        # Parent order -> component demand link (traceability in the Pegging tab).
        if parent_po is not None:
            MRPPegging.objects.create(
                tenant=run.tenant, planned_order=parent_po, demand=demand,
                pegged_quantity=req, required_date=components_required_date,
                supply_date=parent_po.planned_receipt_date, shortage_quantity=ZERO)

        _plan_component(run, comp, site, req, components_required_date, parent_po, demand,
                        depth, ancestors, line_phantom=bool(getattr(line, "is_phantom", False)))


def _plan_component(run, comp, site, req, required_date, nearest_po, demand,
                    depth, ancestors, line_phantom=False):
    from core.models import ItemSitePlanning, MRPPegging
    from core.services.mrp import engine

    if depth >= MAX_BOM_DEPTH:
        mrp_exc.raise_exception(
            run, "BOM_MAX_DEPTH_EXCEEDED",
            f"Max BOM depth ({MAX_BOM_DEPTH}) exceeded at {comp.sku}; stopped exploding.",
            product=comp, site=site)
        return

    if comp.id in ancestors:
        mrp_exc.raise_exception(
            run, "CIRCULAR_BOM",
            f"Circular BOM detected at {comp.sku}; stopped to avoid a loop.",
            product=comp, site=site)
        return

    profile = (ItemSitePlanning.objects
               .filter(tenant=run.tenant, product=comp, site=site, is_active=True)
               .select_related("product", "site", "default_supplier").first())

    phantom = line_phantom or (profile is not None and profile.source_type == "PHANTOM")
    if phantom:
        # Blow through: no planned order for the phantom; explode its own BOM and
        # roll the requirement down to the next level.
        explode_and_plan(run, comp, site, req, required_date, nearest_po,
                         depth + 1, ancestors | {comp.id}, phantom=True)
        return

    if profile is None or not profile.mrp_enabled:
        mrp_exc.raise_exception(
            run, "MISSING_COMPONENT_PLANNING",
            f"No active MRP planning profile for component {comp.sku} at {site.name}.",
            product=comp, site=site)
        return

    # Net the dependent demand against the component's own stock/supply.
    available = engine.take_component_availability(run, profile, req)
    net = req - available
    if net <= ZERO:
        return  # covered by existing stock/supply

    qty, capped, notes = lot_sizing.size_order(profile, net)
    if qty <= ZERO:
        return

    po = engine.create_planned_order(
        run, profile, qty, required_date, profile.lead_time_days or 0,
        profile.source_type, parent_po=nearest_po)

    # Component planned order -> component demand link.
    MRPPegging.objects.create(
        tenant=run.tenant, planned_order=po, demand=demand,
        pegged_quantity=min(qty, req), required_date=required_date,
        supply_date=po.planned_receipt_date, shortage_quantity=ZERO)

    for code, msg in notes:
        mrp_exc.raise_exception(run, code, msg, product=comp, site=site, planned_order=po,
                                dedupe_key=(code, profile.id))
        mrp_exc.bump_level(po, "INFO")
    engine.order_exceptions(run, profile, po, run.planning_start_date)

    if capped:
        mrp_exc.raise_exception(
            run, "SHORTAGE",
            f"{comp.sku}: max order qty too small to fully cover component demand.",
            product=comp, site=site, planned_order=po)

    if profile.source_type == "MAKE":
        explode_and_plan(run, comp, site, po.quantity, po.planned_release_date, po,
                         depth + 1, ancestors | {comp.id})
