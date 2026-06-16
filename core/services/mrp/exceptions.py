"""MRP exception creation.

A single ``raise_exception`` helper records planner-facing issues on a run
without ever crashing the run. The engine and collectors call it for bad or
inferred data and then keep going. A small registry gives each code a default
severity, message and recommended action so call sites stay terse.
"""
from django.utils import timezone

# Default (severity, recommended_action) per exception code. Codes mirror
# core.models.MRPException.Code; using the string values keeps this import-light.
_REGISTRY = {
    "SHORTAGE": ("CRITICAL", "Expedite supply or review demand."),
    "PAST_DUE_DEMAND": ("WARNING", "Demand date is in the past; review and reschedule."),
    "PAST_DUE_SUPPLY": ("WARNING", "Scheduled receipt is overdue; chase the supplier."),
    "PAST_DUE_RELEASE": ("WARNING", "Release date already passed; expedite this order."),
    "BELOW_SAFETY_STOCK": ("WARNING", "Projected stock falls below safety stock."),
    "MISSING_SUPPLIER": ("WARNING", "Set a default supplier on the item planning profile."),
    "MISSING_LEAD_TIME": ("WARNING", "Set a lead time on the item planning profile."),
    "MISSING_UOM_CONVERSION": ("WARNING", "Add the missing UOM conversion; quantity used as-is."),
    "NON_NETTABLE_STOCK": ("INFO", "Stock in quarantine/damaged/transit was excluded from availability."),
    "SALES_ORDER_REQUIRED_DATE_MISSING": ("WARNING", "Sales order has no required date; order date was used."),
    "PURCHASE_ORDER_DUE_DATE_MISSING": ("WARNING", "PO has no expected date; planning start date was used."),
    "UNSUPPORTED_LOT_SIZING_METHOD": ("INFO", "Lot sizing method not supported yet; lot-for-lot used."),
    "EXPIRED_LOT": ("INFO", "Expired lot quantity was excluded from availability."),
    # Phase 3 (BOM / MAKE)
    "MISSING_BOM": ("WARNING", "Create an active BOM for this make item."),
    "INVALID_BOM": ("WARNING", "BOM has no usable lines or invalid data."),
    "BOM_NOT_APPROVED": ("WARNING", "Approve the BOM before planning."),
    "BOM_NOT_EFFECTIVE": ("WARNING", "BOM is not effective for the required date."),
    "CIRCULAR_BOM": ("CRITICAL", "Break the circular BOM reference."),
    "BOM_MAX_DEPTH_EXCEEDED": ("CRITICAL", "BOM nesting is too deep; review the structure."),
    "MISSING_COMPONENT_PLANNING": ("WARNING", "Create an item planning profile for this component."),
    "PHANTOM_BOM_MISSING": ("WARNING", "Phantom item has no BOM to explode."),
    # Phase 4 (transfer planning)
    "MISSING_TRANSFER_SOURCE_SITE": ("WARNING", "Set a default transfer-from site on the planning profile."),
    "INVALID_TRANSFER_SOURCE_SITE": ("WARNING", "Choose a valid, active source site different from the destination."),
    "SOURCE_SITE_SHORTAGE": ("WARNING", "Source site cannot fully cover the transfer; it is being planned."),
    "TRANSFER_LOOP_DETECTED": ("CRITICAL", "Break the circular transfer sourcing between sites."),
    "TRANSFER_MAX_DEPTH_EXCEEDED": ("CRITICAL", "Transfer sourcing chain is too deep; review the network."),
    "INBOUND_TRANSFER_DUE_DATE_MISSING": ("INFO", "Transfer has no due date; planning start date was used."),
    # Phase 9 (forecast + consumption)
    "FORECAST_VERSION_MISSING": ("WARNING", "Select a forecast version on the MRP run."),
    "FORECAST_VERSION_INVALID": ("WARNING", "Choose an Active or Locked forecast version."),
    "FORECAST_LINE_OUTSIDE_HORIZON": ("INFO", "Forecast line falls outside the planning window; ignored."),
    "FORECAST_LINE_INVALID_QTY": ("WARNING", "Fix the forecast line quantity (must be >= 0)."),
    "UNSUPPORTED_FORECAST_CONSUMPTION_METHOD": ("INFO", "Consumption method not supported yet; forecast not consumed."),
    "FORECAST_UOM_CONVERSION_MISSING": ("WARNING", "Add the missing forecast UOM conversion."),
    "FORECAST_SITE_MISSING": ("WARNING", "Set a site on the forecast line."),
}


def raise_exception(run, code, message, *, product=None, site=None, planned_order=None,
                    severity=None, recommended_action=None,
                    source_document_type="", source_document_id="", dedupe_key=None):
    """Create an MRPException for ``run``. Returns the created row (or the
    existing one when ``dedupe_key`` matches an already-recorded exception).

    ``dedupe_key`` (any hashable, tracked on ``run`` for the duration of the
    engine call) suppresses duplicates such as one-warning-per-sales-order.
    """
    from core.models import MRPException

    default_sev, default_action = _REGISTRY.get(code, ("WARNING", ""))
    sev = severity or default_sev
    action = recommended_action if recommended_action is not None else default_action

    if dedupe_key is not None:
        seen = getattr(run, "_mrp_exc_seen", None)
        if seen is None:
            seen = set()
            run._mrp_exc_seen = seen
        full_key = (code, dedupe_key)
        if full_key in seen:
            return None
        seen.add(full_key)

    exc = MRPException.objects.create(
        mrp_run=run, tenant=run.tenant, product=product, site=site, planned_order=planned_order,
        exception_code=code, severity=sev, message=message, recommended_action=action,
        source_document_type=source_document_type or "", source_document_id=source_document_id or "",
        created_at=timezone.now(),
    )
    # Bubble the worst severity onto the linked planned order for the workbench.
    if planned_order is not None:
        _bump_planned_order_level(planned_order, sev)
    return exc


_LEVEL_RANK = {"NONE": 0, "INFO": 1, "WARNING": 2, "CRITICAL": 3}


def _bump_planned_order_level(planned_order, severity):
    current = _LEVEL_RANK.get(planned_order.exception_level, 0)
    incoming = _LEVEL_RANK.get(severity, 0)
    if incoming > current:
        planned_order.exception_level = severity
        planned_order.save(update_fields=["exception_level"])


# Public alias - callers raise a deduped exception once per profile but still
# want every affected planned order's level reflected.
bump_level = _bump_planned_order_level
