"""Planner dashboard metrics + cross-run comparison (Phase 17).

Read-only, query-efficient aggregates over the latest MRP run, open suggestions,
capacity overloads and forecast/sales demand. Reuses the Phase 15 run summary.
No planning changes.
"""
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

ZERO = Decimal("0.00")
_CAPACITY_CODES = ["CAPACITY_OVERLOAD", "FINITE_CAPACITY_OVERLOAD"]


def latest_two_runs(tenant):
    from core.models import MRPRun
    return list(MRPRun.objects.filter(tenant=tenant).order_by("-created_at")[:2])


def latest_mrp_summary(tenant):
    from core.services.mrp import reports
    runs = latest_two_runs(tenant)
    if not runs:
        return None
    return {"run": runs[0], "summary": reports.mrp_run_summary(runs[0])}


def open_suggestions_summary(tenant):
    from core.models import MRPRescheduleSuggestion
    qs = MRPRescheduleSuggestion.objects.filter(tenant=tenant, status__in=["SUGGESTED", "ACCEPTED"])
    agg = qs.aggregate(
        total=Count("id"),
        critical=Count("id", filter=Q(severity="CRITICAL")),
        capacity=Count("id", filter=Q(suggestion_type="CAPACITY_LEVEL")),
        expedite=Count("id", filter=Q(suggestion_type="EXPEDITE")),
    )
    return agg


def critical_exception_list(tenant, run=None, limit=10):
    from core.models import MRPException, MRPRun
    if run is None:
        run = MRPRun.objects.filter(tenant=tenant).order_by("-created_at").first()
    if run is None:
        return []
    return list(run.exceptions.filter(severity="CRITICAL")
                .select_related("product", "site", "planned_order").order_by("exception_code")[:limit])


def past_due_release_list(tenant, run=None, limit=10):
    from core.models import MRPRun
    today = timezone.localdate()
    if run is None:
        run = MRPRun.objects.filter(tenant=tenant).order_by("-created_at").first()
    if run is None:
        return []
    return list(run.planned_orders.filter(planned_release_date__lt=today,
                                          status__in=["SUGGESTED", "FIRMED"])
                .select_related("product", "site").order_by("planned_release_date")[:limit])


def capacity_overload_summary(tenant, run=None):
    from core.models import MRPRun
    if run is None:
        run = MRPRun.objects.filter(tenant=tenant).order_by("-created_at").first()
    if run is None:
        return {"count": 0, "rows": []}
    excs = list(run.exceptions.filter(exception_code__in=_CAPACITY_CODES)
                .select_related("site").order_by("severity")[:20])
    return {"count": len(excs), "rows": excs}


def forecast_vs_sales_summary(tenant, run=None):
    from core.models import MRPRun
    if run is None:
        run = MRPRun.objects.filter(tenant=tenant).order_by("-created_at").first()
    if run is None:
        return {"forecast": ZERO, "sales": ZERO}
    agg = run.demands.aggregate(
        forecast=Sum("open_quantity", filter=Q(demand_type="FORECAST")),
        sales=Sum("open_quantity", filter=Q(demand_type="SALES_ORDER")),
    )
    return {"forecast": agg["forecast"] or ZERO, "sales": agg["sales"] or ZERO}


def work_orders_due_this_week(tenant):
    from core.models import WorkOrder
    today = timezone.localdate()
    import datetime
    week_end = today + datetime.timedelta(days=7)
    return WorkOrder.objects.filter(
        tenant=tenant, status__in=["PLANNED", "FIRM", "RELEASED", "PARTIALLY_COMPLETED"],
        required_date__gte=today, required_date__lte=week_end).count()


def cross_run_comparison(tenant):
    """Latest vs previous run on key metrics. Returns None when fewer than two
    runs exist."""
    from core.services.mrp import reports
    runs = latest_two_runs(tenant)
    if len(runs) < 2:
        return None
    latest, previous = reports.mrp_run_summary(runs[0]), reports.mrp_run_summary(runs[1])
    keys = [
        ("total_planned", "Planned orders"), ("converted", "Converted"),
        ("critical_exceptions", "Critical exceptions"), ("past_due_releases", "Past-due releases"),
        ("capacity_overload_count", "Capacity overloads"), ("shortage_qty", "Shortage qty"),
        ("forecast_demand_qty", "Forecast demand"), ("sales_demand_qty", "Sales demand"),
    ]
    rows = []
    for key, label in keys:
        cur = latest.get(key) or 0
        prev = previous.get(key) or 0
        rows.append({"label": label, "current": cur, "previous": prev, "change": cur - prev})
    return {"latest_run": runs[0], "previous_run": runs[1], "rows": rows}


def planner_dashboard_metrics(tenant, filters=None):
    """Everything the dashboard view needs in one call."""
    from core.models import MRPRun
    run = MRPRun.objects.filter(tenant=tenant).order_by("-created_at").first()
    latest = latest_mrp_summary(tenant)
    return {
        "run": run,
        "summary": latest["summary"] if latest else None,
        "suggestions": open_suggestions_summary(tenant),
        "critical_exceptions": critical_exception_list(tenant, run=run),
        "past_due_releases": past_due_release_list(tenant, run=run),
        "capacity_overloads": capacity_overload_summary(tenant, run=run),
        "forecast_vs_sales": forecast_vs_sales_summary(tenant, run=run),
        "work_orders_due": work_orders_due_this_week(tenant),
        "comparison": cross_run_comparison(tenant),
    }
