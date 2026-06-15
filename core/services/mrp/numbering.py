"""Human-readable, tenant-unique document numbers for MRP records.

Mirrors the timestamp-based scheme used elsewhere (e.g. requisition numbers),
which is collision-safe per tenant without a sequence table. Both helpers are
guarded against the (extremely unlikely) clash so callers can rely on the
returned value being free within the tenant.
"""
from django.utils import timezone


def _stamp():
    return timezone.now().strftime("%Y%m%d-%H%M%S-%f")


def next_run_number(tenant):
    """Return an unused MRP run number for ``tenant`` (e.g. ``MRP-20260615-...``)."""
    from core.models import MRPRun
    while True:
        candidate = f"MRP-{_stamp()}"
        if not MRPRun.objects.filter(tenant=tenant, run_number=candidate).exists():
            return candidate


def next_planned_order_number(tenant):
    """Return an unused planned-order number for ``tenant`` (e.g. ``PLN-...``)."""
    from core.models import MRPPlannedOrder
    while True:
        candidate = f"PLN-{_stamp()}"
        if not MRPPlannedOrder.objects.filter(tenant=tenant, planned_order_number=candidate).exists():
            return candidate
