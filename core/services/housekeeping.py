"""Periodic sales maintenance: expire stale quotes and generate due recurring
invoices.

Runs from a management command (`run_sales_housekeeping`, for cron/Task
Scheduler) and opportunistically once per day per tenant when a user loads a
sales page - so the app stays current even without an external scheduler.
"""
from django.utils import timezone


def expire_quotes(tenant, today=None):
    """Mark Draft/Sent quotes whose valid_until has passed as Expired."""
    from core.models import SalesQuote
    today = today or timezone.localdate()
    return SalesQuote.objects.filter(
        tenant=tenant, status__in=[SalesQuote.Status.DRAFT, SalesQuote.Status.SENT],
        valid_until__isnull=False, valid_until__lt=today,
    ).update(status=SalesQuote.Status.EXPIRED)


def run_for_tenant(tenant, today=None, user=None, force=False):
    """Run housekeeping for one tenant, at most once per day unless forced."""
    from core.services import recurring
    today = today or timezone.localdate()
    if not force and tenant.last_housekeeping_date == today:
        return None
    expired = expire_quotes(tenant, today)
    generated = recurring.generate_due(tenant=tenant, today=today, user=user)
    tenant.last_housekeeping_date = today
    tenant.save(update_fields=["last_housekeeping_date"])
    return {"expired": expired, "generated": len(generated)}


def run_all(today=None):
    """Run housekeeping for every tenant (used by the cron command)."""
    from core.models import Tenant
    results = {}
    for tenant in Tenant.objects.all():
        results[tenant.id] = run_for_tenant(tenant, today=today, force=True)
    return results


def opportunistic(request):
    """Best-effort once-a-day housekeeping for the active tenant on page load.
    Never raises into the request path."""
    try:
        from core.access import get_active_tenant
        tenant = get_active_tenant(request)
        if tenant is not None:
            run_for_tenant(tenant, user=getattr(request, "user", None))
    except Exception:
        pass
