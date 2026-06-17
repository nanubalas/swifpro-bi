"""MRP / planning reporting + analytics (Phase 15).

Read-only reporting over the existing MRP, work-order, forecast and capacity
data. Each function returns a dict with ``columns`` and ``rows`` (display-ready,
reused for both the HTML table and the CSV/XLSX export) and, where useful, a
``kpis`` dict for summary cards. Nothing here changes planning, scheduling, GL or
inventory - it only reads and aggregates. All queries are tenant-scoped by the
run / version / filters passed in.
"""
import datetime
from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

ZERO = Decimal("0.00")

_CAPACITY_CODES = ["CAPACITY_OVERLOAD", "FINITE_CAPACITY_OVERLOAD"]
_SHORTAGE_CODES = ["SHORTAGE", "PAST_DUE_RELEASE", "PAST_DUE_DEMAND"]


def _d(value):
    return value if value is not None else ""


# --------------------------------------------------------------------------- #
# Run summary KPIs
# --------------------------------------------------------------------------- #
def mrp_run_summary(run):
    today = timezone.localdate()
    po = run.planned_orders
    agg = po.aggregate(
        total=Count("id"),
        buy=Count("id", filter=Q(source_type="BUY")),
        make=Count("id", filter=Q(source_type="MAKE")),
        transfer=Count("id", filter=Q(source_type="TRANSFER")),
        subcontract=Count("id", filter=Q(source_type="SUBCONTRACT")),
        converted=Count("id", filter=Q(status="CONVERTED")),
        suggested=Count("id", filter=Q(status="SUGGESTED")),
        critical=Count("id", filter=Q(exception_level="CRITICAL")),
        past_due=Count("id", filter=Q(planned_release_date__lt=today,
                                      status__in=["SUGGESTED", "FIRMED"])),
    )
    demand = run.demands.aggregate(
        forecast=Sum("open_quantity", filter=Q(demand_type="FORECAST")),
        sales=Sum("open_quantity", filter=Q(demand_type="SALES_ORDER")),
        safety=Sum("open_quantity", filter=Q(demand_type="SAFETY_STOCK")),
    )
    from core.models import MRPPegging
    shortage_qty = (MRPPegging.objects.filter(planned_order__mrp_run=run)
                    .aggregate(s=Sum("shortage_quantity"))["s"] or ZERO)
    capacity_overloads = run.exceptions.filter(exception_code__in=_CAPACITY_CODES).count()
    return {
        "total_planned": agg["total"], "buy": agg["buy"], "make": agg["make"],
        "transfer": agg["transfer"], "subcontract": agg["subcontract"],
        "converted": agg["converted"], "suggested": agg["suggested"],
        "critical_exceptions": agg["critical"], "past_due_releases": agg["past_due"],
        "shortage_qty": shortage_qty,
        "forecast_demand_qty": demand["forecast"] or ZERO,
        "sales_demand_qty": demand["sales"] or ZERO,
        "safety_demand_qty": demand["safety"] or ZERO,
        "capacity_overload_count": capacity_overloads,
    }


# --------------------------------------------------------------------------- #
# Planned orders
# --------------------------------------------------------------------------- #
def planned_order_report(run, filters):
    qs = (run.planned_orders.select_related(
              "product", "site", "supplier", "transfer_from_site", "converted_by",
              "created_purchase_requisition", "created_purchase_order",
              "created_transfer_order", "created_work_order")
          .order_by("product__sku", "required_date"))
    if filters.get("source_type"):
        qs = qs.filter(source_type=filters["source_type"])
    if filters.get("status"):
        qs = qs.filter(status=filters["status"])
    if filters.get("site"):
        qs = qs.filter(site_id=filters["site"])
    if filters.get("supplier"):
        qs = qs.filter(supplier_id=filters["supplier"])
    if filters.get("exception_level"):
        qs = qs.filter(exception_level=filters["exception_level"])
    if filters.get("req_from"):
        qs = qs.filter(required_date__gte=filters["req_from"])
    if filters.get("req_to"):
        qs = qs.filter(required_date__lte=filters["req_to"])

    columns = ["Run", "Planned order", "Source", "Item", "Description", "Site",
               "Transfer from", "Supplier", "Quantity", "Required", "Release",
               "Receipt", "Status", "Exception", "Converted document", "Converted by", "Converted at"]
    rows = []
    for p in qs:
        rows.append([
            run.run_number, p.planned_order_number, p.get_source_type_display(),
            p.product.sku, p.product.name, p.site.name,
            p.transfer_from_site.name if p.transfer_from_site_id else "",
            p.supplier.name if p.supplier_id else "", p.quantity, _d(p.required_date),
            _d(p.planned_release_date), _d(p.planned_receipt_date), p.get_status_display(),
            p.get_exception_level_display(), _linked_doc(p),
            (p.converted_by.username if p.converted_by_id else ""),
            (p.converted_at.strftime("%Y-%m-%d %H:%M") if p.converted_at else ""),
        ])
    return {"columns": columns, "rows": rows, "objects": list(qs)}


def _linked_doc(p):
    if p.created_purchase_requisition_id:
        return p.created_purchase_requisition.req_number
    if p.created_purchase_order_id:
        return p.created_purchase_order.po_number
    if p.created_transfer_order_id:
        return p.created_transfer_order.transfer_number
    if p.created_work_order_id:
        return p.created_work_order.work_order_number
    return ""


# --------------------------------------------------------------------------- #
# Demand vs supply (time-phased reconstruction)
# --------------------------------------------------------------------------- #
def demand_supply_report(run, filters):
    from core.models import MRPDemand, MRPSupply, MRPPlannedOrder
    site = filters.get("site")
    product = filters.get("product")

    dem = MRPDemand.objects.filter(mrp_run=run).select_related("product", "site")
    sup = MRPSupply.objects.filter(mrp_run=run).select_related("product", "site")
    pos = MRPPlannedOrder.objects.filter(mrp_run=run).select_related("product", "site")
    if site:
        dem, sup, pos = dem.filter(site_id=site), sup.filter(site_id=site), pos.filter(site_id=site)
    if product:
        dem, sup, pos = dem.filter(product_id=product), sup.filter(product_id=product), pos.filter(product_id=product)

    # Group everything by (product, site).
    keys = {}

    def _key(obj):
        k = (obj.product_id, obj.site_id)
        keys.setdefault(k, (obj.product, obj.site))
        return k

    demand_by = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: ZERO)))  # key -> date -> type -> qty
    for d in dem:
        demand_by[_key(d)][d.required_date][d.demand_type] += (d.open_quantity or ZERO)
    supply_by = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: ZERO)))  # key -> date -> type -> qty
    onhand_by = defaultdict(lambda: ZERO)
    for s in sup:
        if s.supply_type == "ON_HAND":
            onhand_by[_key(s)] += (s.available_quantity or ZERO)
        else:
            day = s.receipt_date or run.planning_start_date
            supply_by[_key(s)][day][s.supply_type] += (s.available_quantity or ZERO)
    planned_by = defaultdict(lambda: defaultdict(lambda: ZERO))  # key -> date -> qty
    for p in pos:
        day = p.planned_receipt_date or p.required_date
        planned_by[_key(p)][day] += (p.quantity or ZERO)

    columns = ["Item", "Site", "Date", "Opening PA", "Gross demand", "Sales", "Forecast",
               "Safety stock", "WO component", "Transfer demand", "Scheduled supply",
               "On hand", "PO", "PR", "Transfer supply", "Planned supply",
               "Projected available", "Net requirement"]
    rows = []
    for k in sorted(keys, key=lambda x: keys[x][0].sku):
        product_obj, site_obj = keys[k]
        dates = sorted(set(demand_by[k]) | set(supply_by[k]) | set(planned_by[k]))
        projected = onhand_by[k]
        first = True
        for day in dates:
            dt = demand_by[k].get(day, {})
            st = supply_by[k].get(day, {})
            sales = dt.get("SALES_ORDER", ZERO)
            forecast = dt.get("FORECAST", ZERO)
            safety = dt.get("SAFETY_STOCK", ZERO)
            wo_comp = dt.get("WORK_ORDER_COMPONENT", ZERO)
            transfer_d = dt.get("TRANSFER_REQUEST", ZERO)
            gross = sales + forecast + wo_comp + transfer_d
            po_s = st.get("PURCHASE_ORDER", ZERO)
            pr_s = st.get("PURCHASE_REQUISITION", ZERO)
            tr_s = st.get("TRANSFER_ORDER", ZERO) + st.get("IN_TRANSIT", ZERO)
            wo_s = st.get("WORK_ORDER", ZERO)
            planned_s = planned_by[k].get(day, ZERO)
            scheduled = po_s + pr_s + tr_s + wo_s
            opening = projected
            projected = projected + scheduled + planned_s - gross
            net_req = (safety - projected) if projected < safety else ZERO
            rows.append([
                product_obj.sku, site_obj.name, day, opening, gross, sales, forecast, safety,
                wo_comp, transfer_d, scheduled, (onhand_by[k] if first else ZERO),
                po_s, pr_s, tr_s, planned_s, projected, net_req,
            ])
            first = False
    return {"columns": columns, "rows": rows}


# --------------------------------------------------------------------------- #
# Shortage
# --------------------------------------------------------------------------- #
def shortage_report(run, filters):
    from core.models import MRPPegging
    qs = (run.exceptions.filter(exception_code__in=_SHORTAGE_CODES)
          .select_related("product", "site", "planned_order", "planned_order__supplier",
                          "planned_order__transfer_from_site").order_by("severity"))
    if filters.get("site"):
        qs = qs.filter(site_id=filters["site"])
    if filters.get("product"):
        qs = qs.filter(product_id=filters["product"])
    today = timezone.localdate()
    # Pre-aggregate pegging shortage per planned order.
    short_by_po = defaultdict(lambda: ZERO)
    for peg in MRPPegging.objects.filter(planned_order__mrp_run=run, shortage_quantity__gt=0):
        short_by_po[peg.planned_order_id] += (peg.shortage_quantity or ZERO)

    columns = ["Item", "Site", "Required", "Shortage qty", "Source demand", "Suggested action",
               "Planned order", "Supplier / source site", "Severity", "Days late / until"]
    rows = []
    for e in qs:
        po = e.planned_order
        required = po.required_date if po else None
        days = (required - today).days if required else ""
        if po:
            source = (po.supplier.name if po.supplier_id else
                      (po.transfer_from_site.name if po.transfer_from_site_id else ""))
        else:
            source = ""
        rows.append([
            (e.product.sku if e.product_id else ""), (e.site.name if e.site_id else ""),
            _d(required), short_by_po.get(po.id, "") if po else "",
            (e.source_document_id or e.message), e.recommended_action,
            (po.planned_order_number if po else ""), source, e.get_severity_display(), days,
        ])
    return {"columns": columns, "rows": rows}


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
def exception_report(run, filters):
    qs = (run.exceptions.select_related("product", "site", "planned_order")
          .order_by("severity", "exception_code"))
    if filters.get("severity"):
        qs = qs.filter(severity=filters["severity"])
    if filters.get("exception_code"):
        qs = qs.filter(exception_code=filters["exception_code"])
    if filters.get("site"):
        qs = qs.filter(site_id=filters["site"])
    if filters.get("product"):
        qs = qs.filter(product_id=filters["product"])
    if filters.get("resolved") == "yes":
        qs = qs.filter(is_resolved=True)
    elif filters.get("resolved") == "no":
        qs = qs.filter(is_resolved=False)
    if filters.get("source_type"):
        qs = qs.filter(planned_order__source_type=filters["source_type"])

    columns = ["Run", "Code", "Severity", "Item", "Site", "Planned order", "Source document",
               "Message", "Recommended action", "Resolved", "Created"]
    rows = []
    for e in qs:
        rows.append([
            run.run_number, e.get_exception_code_display(), e.get_severity_display(),
            (e.product.sku if e.product_id else ""), (e.site.name if e.site_id else ""),
            (e.planned_order.planned_order_number if e.planned_order_id else ""),
            (e.source_document_id or ""), e.message, e.recommended_action,
            ("Yes" if e.is_resolved else "No"),
            (e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else ""),
        ])
    return {"columns": columns, "rows": rows}


# --------------------------------------------------------------------------- #
# Pegging
# --------------------------------------------------------------------------- #
def pegging_report(run, filters):
    from core.models import MRPPegging
    qs = (MRPPegging.objects.filter(planned_order__mrp_run=run)
          .select_related("planned_order", "planned_order__product", "planned_order__site",
                          "demand", "demand__product", "demand__site")
          .order_by("planned_order__product__sku"))
    if filters.get("site"):
        qs = qs.filter(planned_order__site_id=filters["site"])
    if filters.get("source_type"):
        qs = qs.filter(planned_order__source_type=filters["source_type"])

    columns = ["Planned order", "PO source", "PO item", "PO site", "Demand type",
               "Demand source", "Demand item", "Demand site", "Pegged qty",
               "Required", "Supply date", "Shortage qty"]
    rows = []
    for g in qs:
        po, dem = g.planned_order, g.demand
        rows.append([
            po.planned_order_number, po.get_source_type_display(), po.product.sku, po.site.name,
            dem.get_demand_type_display(), (dem.source_document_id or ""),
            dem.product.sku, dem.site.name, g.pegged_quantity,
            _d(g.required_date), _d(g.supply_date), g.shortage_quantity,
        ])
    return {"columns": columns, "rows": rows}


# --------------------------------------------------------------------------- #
# Forecast consumption
# --------------------------------------------------------------------------- #
def forecast_consumption_report(version, filters):
    from core.services.mrp import forecast_consumption as fc
    lines = list(version.lines.select_related("product", "site").order_by("product__sku", "forecast_date"))
    if filters.get("site"):
        lines = [l for l in lines if str(l.site_id) == str(filters["site"])]
    if filters.get("product"):
        lines = [l for l in lines if str(l.product_id) == str(filters["product"])]

    method = version.consumption_method if version.consumption_method in fc.SUPPORTED_METHODS else fc.NONE
    fallback = version.start_date or timezone.localdate()
    # Consume per (product, site) group, mirroring the demand collector.
    groups = defaultdict(list)
    for l in lines:
        groups[(l.tenant_id, l.product_id, l.site_id)].append(l)
    consumed_map = {}
    for (tenant_id, product_id, site_id), grp in groups.items():
        for line, remaining, consumed in fc.consume(grp[0].tenant, grp[0].product, grp[0].site,
                                                     grp, method, fallback):
            consumed_map[line.id] = (remaining, consumed)

    columns = ["Forecast version", "Item", "Site", "Bucket", "Forecast date", "Forecast qty",
               "Consumed qty", "Remaining qty", "Consumption method"]
    rows = []
    for l in lines:
        remaining, consumed = consumed_map.get(l.id, (l.quantity or ZERO, ZERO))
        rows.append([
            version.code, l.product.sku, l.site.name, l.get_bucket_type_display(),
            l.forecast_date, (l.quantity or ZERO), consumed, remaining,
            version.get_consumption_method_display(),
        ])
    return {"columns": columns, "rows": rows}


# --------------------------------------------------------------------------- #
# Capacity load
# --------------------------------------------------------------------------- #
def capacity_load_export(tenant, filters):
    from core.models import WorkCentre
    from core.services.mrp import scheduling
    start = filters.get("start") or timezone.localdate()
    end = filters.get("end") or (start + datetime.timedelta(days=13))
    centres = WorkCentre.objects.filter(tenant=tenant, is_active=True).select_related("site")
    if filters.get("site"):
        centres = centres.filter(site_id=filters["site"])
    if filters.get("work_centre"):
        centres = centres.filter(id=filters["work_centre"])
    centres = centres.order_by("code")

    columns = ["Work centre", "Site", "Date", "Available", "Scheduled", "Remaining",
               "Overload", "Utilisation %"]
    rows = []
    tot_avail = tot_sched = tot_over = ZERO
    overloaded_days = 0
    for wc in centres:
        for r in scheduling.calculate_daily_load(wc, start, end):
            rows.append([wc.code, wc.site.name, r["date"], r["available"], r["scheduled"],
                         r["remaining"], r["overload"], r["utilisation"]])
            tot_avail += r["available"]
            tot_sched += r["scheduled"]
            tot_over += r["overload"]
            if r["overload"] > 0:
                overloaded_days += 1
    avg_util = (tot_sched / tot_avail * Decimal("100")).quantize(Decimal("0.1")) if tot_avail > 0 else ZERO
    kpis = {"available": tot_avail, "scheduled": tot_sched, "overload": tot_over,
            "avg_utilisation": avg_util, "overloaded_days": overloaded_days}
    return {"columns": columns, "rows": rows, "kpis": kpis}


# --------------------------------------------------------------------------- #
# Work order cost
# --------------------------------------------------------------------------- #
def work_order_cost_report(tenant, filters):
    from core.models import WorkOrder
    qs = (WorkOrder.objects.filter(tenant=tenant)
          .select_related("product", "site", "source_mrp_planned_order").order_by("-created_at"))
    if filters.get("site"):
        qs = qs.filter(site_id=filters["site"])
    if filters.get("product"):
        qs = qs.filter(product_id=filters["product"])
    if filters.get("status"):
        qs = qs.filter(status=filters["status"])
    if filters.get("date_from"):
        qs = qs.filter(created_at__date__gte=filters["date_from"])
    if filters.get("date_to"):
        qs = qs.filter(created_at__date__lte=filters["date_to"])
    if filters.get("has_variance") == "yes":
        qs = qs.filter(variance_journal__isnull=False)
    if filters.get("has_scrap") == "yes":
        qs = qs.filter(scrap_cost__gt=0)

    columns = ["Work order", "Item", "Site", "Status", "Planned qty", "Completed qty",
               "Scrapped qty", "Material WIP", "Labour WIP", "Overhead WIP", "Total WIP",
               "Scrap cost", "Finished goods cost", "Remaining WIP", "MRP planned order",
               "Created", "Closed"]
    rows = []
    totals = defaultdict(lambda: ZERO)
    for wo in qs:
        remaining = (wo.total_wip_cost or ZERO) - (wo.finished_goods_cost or ZERO)
        rows.append([
            wo.work_order_number, wo.product.sku, wo.site.name, wo.get_status_display(),
            wo.quantity, wo.quantity_completed, wo.quantity_scrapped,
            wo.wip_material_cost, wo.wip_labour_cost, wo.wip_overhead_cost, wo.total_wip_cost,
            wo.scrap_cost, wo.finished_goods_cost, remaining,
            (wo.source_mrp_planned_order.planned_order_number if wo.source_mrp_planned_order_id else ""),
            (wo.created_at.strftime("%Y-%m-%d") if wo.created_at else ""),
            (wo.closed_at.strftime("%Y-%m-%d") if wo.closed_at else ""),
        ])
        for f in ("wip_material_cost", "wip_labour_cost", "wip_overhead_cost", "scrap_cost",
                  "finished_goods_cost"):
            totals[f] += (getattr(wo, f) or ZERO)
    return {"columns": columns, "rows": rows, "kpis": dict(totals)}


# --------------------------------------------------------------------------- #
# Reschedule suggestions (Phase 16)
# --------------------------------------------------------------------------- #
def reschedule_suggestion_report(run, filters):
    qs = (run.reschedule_suggestions.select_related(
              "product", "site", "work_centre", "applied_by", "rejected_by")
          .order_by("severity", "id"))
    if filters.get("suggestion_type"):
        qs = qs.filter(suggestion_type=filters["suggestion_type"])
    if filters.get("status"):
        qs = qs.filter(status=filters["status"])
    if filters.get("severity"):
        qs = qs.filter(severity=filters["severity"])
    if filters.get("site"):
        qs = qs.filter(site_id=filters["site"])
    if filters.get("work_centre"):
        qs = qs.filter(work_centre_id=filters["work_centre"])
    if filters.get("source_type"):
        qs = qs.filter(source_type=filters["source_type"])

    columns = ["Suggestion", "Type", "Status", "Severity", "Source document", "Item", "Site",
               "Work centre", "Current release", "Current receipt", "Suggested release",
               "Suggested receipt", "Current start", "Suggested start", "Reason", "Impact",
               "Applied by", "Applied at", "Rejected by", "Rejected at"]
    rows = []
    for s in qs:
        rows.append([
            s.suggestion_number, s.get_suggestion_type_display(), s.get_status_display(),
            s.get_severity_display(), s.source_document_id,
            (s.product.sku if s.product_id else ""), (s.site.name if s.site_id else ""),
            (s.work_centre.code if s.work_centre_id else ""),
            _d(s.current_release_date), _d(s.current_receipt_date),
            _d(s.suggested_release_date), _d(s.suggested_receipt_date),
            _d(s.current_start), _d(s.suggested_start), s.reason, s.impact_summary,
            (s.applied_by.username if s.applied_by_id else ""),
            (s.applied_at.strftime("%Y-%m-%d %H:%M") if s.applied_at else ""),
            (s.rejected_by.username if s.rejected_by_id else ""),
            (s.rejected_at.strftime("%Y-%m-%d %H:%M") if s.rejected_at else ""),
        ])
    return {"columns": columns, "rows": rows, "objects": list(qs)}


# --------------------------------------------------------------------------- #
# Report dispatcher (Phase 17): one entry point used by scheduled exports.
# Resolves the run / version from filters and returns columns + rows + title.
# --------------------------------------------------------------------------- #
REPORT_TITLES = {
    "RUN_SUMMARY": ("Run summary", "mrp-run-summary"),
    "PLANNED_ORDERS": ("Planned orders", "mrp-planned-orders"),
    "DEMAND_SUPPLY": ("Demand vs supply", "mrp-demand-supply"),
    "SHORTAGES": ("Shortages", "mrp-shortages"),
    "EXCEPTIONS": ("Exceptions", "mrp-exceptions"),
    "PEGGING": ("Pegging", "mrp-pegging"),
    "FORECAST_CONSUMPTION": ("Forecast consumption", "forecast-consumption"),
    "CAPACITY_LOAD": ("Capacity load", "capacity-load"),
    "WORK_ORDER_COST": ("Work order cost", "work-order-cost"),
    "RESCHEDULE_SUGGESTIONS": ("Reschedule suggestions", "reschedule-suggestions"),
    "MRP_WORKBENCH": ("Planned orders (workbench)", "mrp-workbench"),
    "MATERIAL_ORDER_SHEET": ("MRP Material Order Sheet", "mrp-material-order-sheet"),
}

_RUN_SCOPED = {"RUN_SUMMARY", "PLANNED_ORDERS", "DEMAND_SUPPLY", "SHORTAGES", "EXCEPTIONS",
               "PEGGING", "RESCHEDULE_SUGGESTIONS", "MRP_WORKBENCH", "MATERIAL_ORDER_SHEET"}


def _latest_run(tenant, filters):
    from core.models import MRPRun
    qs = MRPRun.objects.filter(tenant=tenant).order_by("-created_at")
    rid = (filters or {}).get("run")
    return (qs.filter(id=rid).first() if rid else None) or qs.first()


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        return None


def material_order_sheet(run, filters):
    """Material Order Sheet - one row per exploded component for the run, in the
    handwritten material-planning layout: placement/component, value, package,
    make, unit qty (BOM qty per FG), current nettable stock, gross required qty,
    planned BUY order qty and order (release) date. Report-only; no MRP change."""
    from core.models import MRPDemand, MRPPlannedOrder, BillOfMaterialsLine
    from core.services.mrp import inventory_snapshot

    site_f = filters.get("site")
    dem = (MRPDemand.objects.filter(mrp_run=run, demand_type="WORK_ORDER_COMPONENT")
           .select_related("product", "site").order_by("product__sku"))
    if site_f:
        dem = dem.filter(site_id=site_f)

    # Aggregate gross required per (component, site); remember a BOM line for unit qty.
    agg, order = {}, []
    for d in dem:
        key = (d.product_id, d.site_id)
        if key not in agg:
            agg[key] = {"product": d.product, "site": d.site, "required": ZERO,
                        "line_id": d.source_line_id}
            order.append(key)
        agg[key]["required"] += (d.open_quantity or ZERO)

    # Planned BUY quantity + earliest release date per (component, site).
    buy = {}
    for p in MRPPlannedOrder.objects.filter(mrp_run=run, source_type="BUY").select_related("product"):
        e = buy.setdefault((p.product_id, p.site_id), {"qty": ZERO, "release": None})
        e["qty"] += (p.quantity or ZERO)
        if p.planned_release_date and (e["release"] is None or p.planned_release_date < e["release"]):
            e["release"] = p.planned_release_date

    columns = ["S.No", "Placement / Component", "Value", "Package", "Make", "Unit Qty",
               "Current Stock", "Required Qty", "Order Qty", "Order Date"]
    rows = []
    for i, key in enumerate(order, start=1):
        a = agg[key]
        prod = a["product"]
        unit_qty = ""
        if a["line_id"] and str(a["line_id"]).isdigit():
            bl = BillOfMaterialsLine.objects.filter(id=int(a["line_id"])).first()
            if bl is not None:
                unit_qty = bl.qty
        avail, _exc, _exp = inventory_snapshot.nettable_on_hand(
            run.tenant, prod, a["site"], as_of=run.planning_start_date)
        b = buy.get(key, {})
        rows.append([
            i, f"{prod.sku} - {prod.name}",
            prod.option1 or "", prod.pack_size or "", prod.brand or "",
            unit_qty, avail, a["required"], b.get("qty", ZERO),
            _d(b["release"]) if b.get("release") else "",
        ])
    return {"columns": columns, "rows": rows}


def build_report(tenant, report_type, filters):
    """Return {title, filename, columns, rows} for any planning report type,
    tenant-scoped, resolving run/version from filters. Used by report views'
    export and by scheduled exports."""
    filters = dict(filters or {})
    title, filename = REPORT_TITLES.get(report_type, (report_type, report_type.lower()))

    if report_type in _RUN_SCOPED:
        run = _latest_run(tenant, filters)
        if run is None:
            return {"title": title, "filename": filename, "columns": ["Message"],
                    "rows": [["No MRP runs available."]]}
        if report_type == "RUN_SUMMARY":
            kpis = mrp_run_summary(run)
            cols = ["Metric", "Value"]
            label = {
                "total_planned": "Total planned", "buy": "Buy", "make": "Make",
                "transfer": "Transfer", "subcontract": "Subcontract", "converted": "Converted",
                "suggested": "Suggested", "critical_exceptions": "Critical exceptions",
                "past_due_releases": "Past-due releases", "shortage_qty": "Shortage qty",
                "forecast_demand_qty": "Forecast demand", "sales_demand_qty": "Sales demand",
                "safety_demand_qty": "Safety demand", "capacity_overload_count": "Capacity overloads"}
            rows = [[label.get(k, k), v] for k, v in kpis.items()]
            return {"title": title, "filename": filename, "columns": cols, "rows": rows}
        fn = {"PLANNED_ORDERS": planned_order_report, "DEMAND_SUPPLY": demand_supply_report,
              "SHORTAGES": shortage_report, "EXCEPTIONS": exception_report,
              "PEGGING": pegging_report, "RESCHEDULE_SUGGESTIONS": reschedule_suggestion_report,
              "MRP_WORKBENCH": planned_order_report,
              "MATERIAL_ORDER_SHEET": material_order_sheet}[report_type]
        data = fn(run, filters)
        return {"title": title, "filename": filename, "columns": data["columns"], "rows": data["rows"]}

    if report_type == "FORECAST_CONSUMPTION":
        from core.models import ForecastVersion
        vid = filters.get("version")
        versions = ForecastVersion.objects.filter(tenant=tenant).order_by("-created_at")
        version = (versions.filter(id=vid).first() if vid else None) or versions.first()
        if version is None:
            return {"title": title, "filename": filename, "columns": ["Message"],
                    "rows": [["No forecast versions available."]]}
        data = forecast_consumption_report(version, filters)
        return {"title": title, "filename": filename, "columns": data["columns"], "rows": data["rows"]}

    if report_type == "CAPACITY_LOAD":
        f = dict(filters)
        f["start"] = _parse_date(filters.get("start"))
        f["end"] = _parse_date(filters.get("end"))
        data = capacity_load_export(tenant, f)
        return {"title": title, "filename": filename, "columns": data["columns"], "rows": data["rows"]}

    if report_type == "WORK_ORDER_COST":
        data = work_order_cost_report(tenant, filters)
        return {"title": title, "filename": filename, "columns": data["columns"], "rows": data["rows"]}

    return {"title": title, "filename": filename, "columns": ["Message"],
            "rows": [[f"Unknown report type '{report_type}'."]]}
