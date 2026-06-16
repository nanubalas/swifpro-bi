"""MRP setup health check + guided-flow status (Phase 18).

Read-only diagnostics over a tenant's MRP master data. Each check returns a
severity, a count, a few example records and an optional fix link. Nothing here
blocks the user - it just surfaces common broken-setup problems before a run.
"""
from decimal import Decimal


def _examples(items, fmt, limit=5):
    return [fmt(i) for i in items[:limit]]


def run_health_checks(tenant):
    """Return a list of check dicts: key, label, severity, count, examples, fix_url, ok."""
    from core.models import (ItemSitePlanning, BillOfMaterials, RoutingHeader, WorkCentre,
                             ForecastVersion, ManufacturingAccountingProfile, Location, MRPRun)
    checks = []

    def add(key, label, severity, items, fmt, fix_url=None):
        items = list(items)
        checks.append({
            "key": key, "label": label, "severity": severity, "count": len(items),
            "examples": _examples(items, fmt), "fix_url": fix_url, "ok": len(items) == 0,
        })

    enabled = ItemSitePlanning.objects.filter(tenant=tenant, is_active=True, mrp_enabled=True)

    # Items with active BOMs / routings (for MAKE checks).
    make = enabled.filter(source_type="MAKE").select_related("product", "site")
    bom_products = set(BillOfMaterials.objects.filter(tenant=tenant, is_active=True)
                       .values_list("product_id", flat=True))
    routed_products = set(RoutingHeader.objects.filter(tenant=tenant, status="ACTIVE")
                          .values_list("product_id", flat=True))

    add("buy_no_supplier", "BUY items without a default supplier", "WARNING",
        enabled.filter(source_type="BUY", default_supplier__isnull=True).select_related("product", "site"),
        lambda i: f"{i.product.sku} @ {i.site.name}", "item_planning_list")

    add("subcontract_no_supplier", "SUBCONTRACT items without a supplier", "WARNING",
        enabled.filter(source_type="SUBCONTRACT", default_supplier__isnull=True).select_related("product", "site"),
        lambda i: f"{i.product.sku} @ {i.site.name}", "item_planning_list")

    add("make_no_bom", "MAKE items without an active BOM", "CRITICAL",
        [m for m in make if m.product_id not in bom_products],
        lambda i: f"{i.product.sku} @ {i.site.name}", "item_planning_list")

    add("make_no_routing", "MAKE items without an active routing", "WARNING",
        [m for m in make if m.product_id not in routed_products],
        lambda i: f"{i.product.sku} @ {i.site.name}", "routing_list")

    add("transfer_no_source", "TRANSFER items without a source site", "CRITICAL",
        enabled.filter(source_type="TRANSFER", default_transfer_from_site__isnull=True)
        .select_related("product", "site"),
        lambda i: f"{i.product.sku} @ {i.site.name}", "item_planning_list")

    add("no_lead_time", "MRP items with no lead time", "INFO",
        enabled.filter(lead_time_days=0).select_related("product", "site"),
        lambda i: f"{i.product.sku} @ {i.site.name}", "item_planning_list")

    add("wc_no_capacity", "Work centres with no daily capacity", "WARNING",
        WorkCentre.objects.filter(tenant=tenant, is_active=True, capacity_hours_per_day__lte=0),
        lambda w: w.code, "work_centre_list")

    add("wc_finite_no_calendar", "Finite work centres without a calendar", "WARNING",
        WorkCentre.objects.filter(tenant=tenant, is_active=True, finite_capacity_enabled=True,
                                  shop_calendar__isnull=True),
        lambda w: w.code, "work_centre_list")

    # Forecast enabled but no active/locked version.
    forecast_enabled = enabled.filter(include_forecast=True).exists()
    has_active_version = ForecastVersion.objects.filter(
        tenant=tenant, status__in=["ACTIVE", "LOCKED"]).exists()
    add("forecast_no_version",
        "Forecast enabled on items but no active forecast version", "WARNING",
        ([1] if (forecast_enabled and not has_active_version) else []),
        lambda x: "No Active/Locked forecast version", "forecast_version_list")

    has_gl = ManufacturingAccountingProfile.objects.filter(tenant=tenant, is_active=True).exists()
    add("no_gl_profile", "Manufacturing GL profile not configured", "INFO",
        ([] if has_gl else [1]), lambda x: "No active manufacturing accounting profile", None)

    # Sites with MRP planning but no nettable stock location.
    planning_sites = set(enabled.values_list("site_id", flat=True))
    stock_sites = set(Location.objects.filter(tenant=tenant, is_active=True, holds_stock=True)
                      .values_list("site_id", flat=True))
    missing_loc_sites = [s for s in planning_sites if s not in stock_sites]
    from core.models import Site
    add("site_no_location", "Planning sites without a stock location", "WARNING",
        list(Site.objects.filter(id__in=missing_loc_sites)),
        lambda s: s.name, None)

    add("runs_with_critical", "MRP runs with critical exceptions", "WARNING",
        list(MRPRun.objects.filter(tenant=tenant, exceptions__severity="CRITICAL").distinct()
             .order_by("-created_at")[:5]),
        lambda r: r.run_number, "mrp_run_list")

    return checks


# --------------------------------------------------------------------------- #
# Guided setup-flow status
# --------------------------------------------------------------------------- #
def setup_guide_steps(tenant):
    """Return the ordered MRP setup steps with completion status + a link."""
    from core.models import (Site, Location, Product, ItemSitePlanning, Supplier, BillOfMaterials,
                             RoutingHeader, WorkCentre, ForecastVersion, CustomerOrder, MRPRun,
                             MRPPlannedOrder, WorkOrder)

    def has(qs):
        return qs.exists()

    sites_ok = (has(Site.objects.filter(tenant=tenant))
                and has(Location.objects.filter(tenant=tenant, holds_stock=True)))
    converted_ok = has(MRPPlannedOrder.objects.filter(tenant=tenant, status="CONVERTED"))

    steps = [
        (1, "Create sites & locations", sites_ok, "site_list",
         "Set up at least one operating site and a stock-holding location."),
        (2, "Create products", has(Product.objects.filter(tenant=tenant)), "product_list",
         "Add the finished goods, sub-assemblies and raw materials you plan."),
        (3, "Create item planning profiles", has(ItemSitePlanning.objects.filter(tenant=tenant)),
         "item_planning_list", "Tell MRP how each item is sourced (BUY / MAKE / TRANSFER / SUBCONTRACT)."),
        (4, "Create suppliers", has(Supplier.objects.filter(tenant=tenant)), "supplier_list",
         "Add suppliers for BUY and SUBCONTRACT items."),
        (5, "Create BOMs", has(BillOfMaterials.objects.filter(tenant=tenant, is_active=True)),
         "product_list", "Define what each MAKE item is built from."),
        (6, "Create routings & work centres",
         has(RoutingHeader.objects.filter(tenant=tenant)) and has(WorkCentre.objects.filter(tenant=tenant)),
         "routing_list", "Add work centres and routing operations for capacity and costing."),
        (7, "Create forecast or sales demand",
         has(ForecastVersion.objects.filter(tenant=tenant)) or has(CustomerOrder.objects.filter(tenant=tenant)),
         "forecast_version_list", "Give MRP something to plan against - a forecast or confirmed orders."),
        (8, "Run MRP", has(MRPRun.objects.filter(tenant=tenant)), "mrp_run_list",
         "Create an MRP run and execute it to generate planned orders."),
        (9, "Review the planner workbench", has(MRPPlannedOrder.objects.filter(tenant=tenant)),
         "mrp_run_list", "Filter, peg and review planned orders on the workbench."),
        (10, "Convert planned orders", converted_ok, "mrp_run_list",
         "Turn planned orders into requisitions, transfers and work orders."),
        (11, "Execute work orders", has(WorkOrder.objects.filter(tenant=tenant)), "work_order_list",
         "Firm, release, issue material, book labour/overhead and complete."),
        (12, "Review reports & dashboard", True, "mrp_reports_index",
         "Use the planner dashboard and reports to monitor and export."),
    ]
    return [{"n": n, "title": t, "done": d, "url": u, "desc": desc} for (n, t, d, u, desc) in steps]
