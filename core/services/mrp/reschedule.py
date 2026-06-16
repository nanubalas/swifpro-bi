"""Reschedule suggestions + capacity-levelling recommendations (Phase 16).

Generates planner-reviewable suggestions from a run's capacity load, supply
timing and planned-order state, and applies only *safe* ones on request. Nothing
here auto-optimises: suggestions are stored on MRPRescheduleSuggestion and a
planner accepts/applies/rejects them. Apply changes only unconverted planned
orders and PLANNED/FIRM work-order operations; released / completed / closed /
converted documents are never moved silently.

Generation is idempotent: re-running clears the run's open (SUGGESTED)
suggestions and regenerates, and each generator skips a source it has already
created an open suggestion for.
"""
import datetime
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Min
from django.utils import timezone
from django.utils.crypto import get_random_string

ZERO = Decimal("0.00")
RESCHEDULE_OUT_THRESHOLD_DAYS = 7
_CAPACITY_SEARCH_DAYS = 60
_MOVABLE_PO_STATUSES = {"SUGGESTED", "FIRMED", "REVIEWED"}

_SUPPLY_SOURCE = {
    "PURCHASE_ORDER": "PURCHASE_ORDER", "PURCHASE_REQUISITION": "PURCHASE_REQUISITION",
    "TRANSFER_ORDER": "TRANSFER_ORDER", "IN_TRANSIT": "TRANSFER_ORDER",
    "WORK_ORDER": "WORK_ORDER", "PLANNED_ORDER": "MRP_PLANNED_ORDER",
}


class RescheduleError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _number(tenant):
    return "RS-" + timezone.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + get_random_string(4).upper()


def _is_converted(po):
    return (po.status == "CONVERTED" or po.created_purchase_requisition_id
            or po.created_purchase_order_id or po.created_transfer_order_id
            or po.created_work_order_id)


def _exists(run, stype, src_type, src_id, line_id=""):
    from core.models import MRPRescheduleSuggestion
    return MRPRescheduleSuggestion.objects.filter(
        mrp_run=run, suggestion_type=stype, source_document_type=src_type,
        source_document_id=str(src_id), source_line_id=str(line_id or ""),
        status__in=["SUGGESTED", "ACCEPTED"]).exists()


def _create(run, user, **kw):
    from core.models import MRPRescheduleSuggestion
    return MRPRescheduleSuggestion.objects.create(
        tenant=run.tenant, mrp_run=run, suggestion_number=_number(run.tenant),
        created_by=user, **kw)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
@transaction.atomic
def generate_reschedule_suggestions(run, user=None):
    """Regenerate all open suggestions for a run (idempotent). Returns the count."""
    from core.models import MRPRescheduleSuggestion
    MRPRescheduleSuggestion.objects.filter(mrp_run=run, status="SUGGESTED").delete()
    total = 0
    total += generate_capacity_level_suggestions(run, user)
    total += generate_reschedule_in_out_suggestions(run, user)
    total += generate_cancel_supply_suggestions(run, user)
    total += generate_expedite_suggestions(run, user)
    return total


# --------------------------------------------------------------------------- #
# Capacity levelling
# --------------------------------------------------------------------------- #
def _find_capacity_day(work_centre, from_day, hours, earlier=True):
    from core.services.mrp import scheduling
    step = -1 if earlier else 1
    cursor = from_day + datetime.timedelta(days=step)
    for _ in range(_CAPACITY_SEARCH_DAYS):
        if scheduling.is_working_day(work_centre, cursor):
            avail = (scheduling.get_working_capacity(work_centre, cursor)
                     - scheduling.daily_scheduled_hours(work_centre, cursor))
            if avail >= hours:
                return cursor
        cursor += datetime.timedelta(days=step)
    return None


def generate_capacity_level_suggestions(run, user=None):
    from core.models import MRPPlannedOrder
    from core.services.mrp import scheduling, routing_capacity
    created = 0

    contrib = defaultdict(lambda: defaultdict(list))  # wc_id -> day -> [(po, hours)]
    wc_map = {}
    for po in (MRPPlannedOrder.objects.filter(mrp_run=run, source_type="MAKE")
               .select_related("product", "site")):
        routing = routing_capacity.find_active_routing(po.product, po.site, po.required_date)
        if routing is None:
            continue
        day = po.planned_receipt_date or po.required_date
        if day is None:
            continue
        for op in routing.operations.select_related("work_centre").all():
            if op.is_subcontract_operation:
                continue
            wc = op.work_centre
            if wc is None or not wc.is_active:
                continue
            contrib[wc.id][day].append((po, routing_capacity.calculate_operation_hours(op, po.quantity)))
            wc_map[wc.id] = wc

    for wc_id, by_day in contrib.items():
        wc = wc_map[wc_id]
        for day, items in by_day.items():
            available = scheduling.get_working_capacity(wc, day)
            committed = scheduling.daily_scheduled_hours(wc, day)
            total = sum((h for _, h in items), ZERO) + committed
            if total <= available:
                continue
            # Move the lowest-priority (latest required) unconverted planned order.
            movable = [(po, h) for po, h in items
                       if po.status in _MOVABLE_PO_STATUSES and not _is_converted(po)]
            if not movable:
                continue
            movable.sort(key=lambda x: x[0].required_date, reverse=True)
            po, hours = movable[0]
            target = (_find_capacity_day(wc, day, hours, earlier=True)
                      or _find_capacity_day(wc, day, hours, earlier=False))
            if target is None:
                continue
            if _exists(run, "CAPACITY_LEVEL", "MRPPlannedOrder", po.planned_order_number):
                continue
            _create(run, user, suggestion_type="CAPACITY_LEVEL", source_type="MRP_PLANNED_ORDER",
                    source_document_type="MRPPlannedOrder", source_document_id=po.planned_order_number,
                    planned_order=po, product=po.product, site=po.site, work_centre=wc,
                    current_receipt_date=day, current_release_date=po.planned_release_date,
                    suggested_receipt_date=target, suggested_release_date=target,
                    quantity=po.quantity, severity="WARNING",
                    reason=f"{wc.code} overloaded on {day}: {total} h scheduled vs {available} h capacity.",
                    impact_summary=f"Move {po.planned_order_number} to {target}.")
            created += 1
    return created


# --------------------------------------------------------------------------- #
# Reschedule in / out (supply timing vs demand)
# --------------------------------------------------------------------------- #
def generate_reschedule_in_out_suggestions(run, user=None):
    from core.models import MRPSupply, MRPDemand, MRPPlannedOrder
    created = 0
    earliest = {}
    for d in (MRPDemand.objects.filter(mrp_run=run)
              .values("product_id", "site_id").annotate(m=Min("required_date"))):
        earliest[(d["product_id"], d["site_id"])] = d["m"]

    for s in (MRPSupply.objects.filter(mrp_run=run).exclude(supply_type="ON_HAND")
              .select_related("product", "site")):
        req = earliest.get((s.product_id, s.site_id))
        if req is None or s.receipt_date is None:
            continue
        if s.receipt_date > req:
            stype, sev = "RESCHEDULE_IN", "WARNING"
            reason = (f"Supply {s.source_document_id or s.get_supply_type_display()} arrives "
                      f"{s.receipt_date}, after demand needed {req}.")
        elif s.receipt_date < req - datetime.timedelta(days=RESCHEDULE_OUT_THRESHOLD_DAYS):
            stype, sev = "RESCHEDULE_OUT", "INFO"
            reason = (f"Supply {s.source_document_id or s.get_supply_type_display()} arrives "
                      f"{s.receipt_date}, more than {RESCHEDULE_OUT_THRESHOLD_DAYS} days before "
                      f"demand needed {req}.")
        else:
            continue
        src_doc_type = s.source_document_type or s.supply_type
        src_doc_id = s.source_document_id or str(s.id)
        if _exists(run, stype, src_doc_type, src_doc_id, s.source_line_id):
            continue
        po_link = None
        if s.supply_type == "PLANNED_ORDER" and s.source_document_id:
            po_link = MRPPlannedOrder.objects.filter(
                mrp_run=run, planned_order_number=s.source_document_id).first()
        _create(run, user, suggestion_type=stype,
                source_type=_SUPPLY_SOURCE.get(s.supply_type, "PURCHASE_ORDER"),
                source_document_type=src_doc_type, source_document_id=src_doc_id,
                source_line_id=(s.source_line_id or ""), planned_order=po_link,
                product=s.product, site=s.site, current_receipt_date=s.receipt_date,
                suggested_receipt_date=req, quantity=s.quantity, severity=sev, reason=reason)
        created += 1
    return created


# --------------------------------------------------------------------------- #
# Cancel unneeded supply
# --------------------------------------------------------------------------- #
def generate_cancel_supply_suggestions(run, user=None):
    from core.models import MRPPlannedOrder
    created = 0
    for po in (MRPPlannedOrder.objects.filter(mrp_run=run, status__in=["SUGGESTED", "FIRMED"])
               .select_related("product", "site")):
        if _is_converted(po) or po.peggings.exists():
            continue
        if _exists(run, "CANCEL_SUPPLY", "MRPPlannedOrder", po.planned_order_number):
            continue
        _create(run, user, suggestion_type="CANCEL_SUPPLY", source_type="MRP_PLANNED_ORDER",
                source_document_type="MRPPlannedOrder", source_document_id=po.planned_order_number,
                planned_order=po, product=po.product, site=po.site, quantity=po.quantity,
                severity="INFO", reason="Planned order is not pegged to any demand.",
                impact_summary="Cancel to remove unneeded supply.")
        created += 1
    return created


# --------------------------------------------------------------------------- #
# Expedite past-due / impossible releases
# --------------------------------------------------------------------------- #
def generate_expedite_suggestions(run, user=None):
    from core.models import MRPPlannedOrder
    today = timezone.localdate()
    floor = run.planning_start_date or today
    created = 0
    for po in (MRPPlannedOrder.objects.filter(mrp_run=run, status__in=["SUGGESTED", "FIRMED"])
               .select_related("product", "site")):
        rel = po.planned_release_date
        if rel is None or rel >= max(floor, today):
            continue
        sev = "CRITICAL" if rel < today else "WARNING"
        if _exists(run, "EXPEDITE", "MRPPlannedOrder", po.planned_order_number):
            continue
        _create(run, user, suggestion_type="EXPEDITE", source_type="MRP_PLANNED_ORDER",
                source_document_type="MRPPlannedOrder", source_document_id=po.planned_order_number,
                planned_order=po, product=po.product, site=po.site,
                current_release_date=rel, current_receipt_date=po.planned_receipt_date,
                quantity=po.quantity, severity=sev,
                reason=f"Planned release {rel} is in the past; expedite to meet {po.required_date}.",
                impact_summary="Expedite supply.")
        created += 1
    return created


# --------------------------------------------------------------------------- #
# Apply / reject
# --------------------------------------------------------------------------- #
@transaction.atomic
def apply_suggestion(sug, user):
    if sug.status not in ("SUGGESTED", "ACCEPTED"):
        raise RescheduleError("ALREADY_RESOLVED", f"Suggestion is already {sug.get_status_display()}.")
    if sug.planned_order_id:
        _apply_planned_order(sug)
    elif sug.work_order_operation_id:
        _apply_work_order_operation(sug)
    else:
        raise RescheduleError(
            "NOT_APPLICABLE",
            "This suggestion is advisory only (external document); apply it on the source document.")
    sug.status = "APPLIED"
    sug.applied_by = user
    sug.applied_at = timezone.now()
    sug.save(update_fields=["status", "applied_by", "applied_at"])
    return sug


def _apply_planned_order(sug):
    po = sug.planned_order
    if po.status in ("CONVERTED", "CANCELLED", "IGNORED", "EXPIRED"):
        raise RescheduleError("UNSAFE", f"Planned order is {po.get_status_display()}; cannot change it.")
    t = sug.suggestion_type
    if t == "CANCEL_SUPPLY":
        if _is_converted(po):
            raise RescheduleError("UNSAFE", "Planned order has a downstream document; cannot cancel.")
        po.status = "CANCELLED"
        po.save(update_fields=["status"])
    elif t == "EXPEDITE":
        po.action_type = "EXPEDITE"
        po.save(update_fields=["action_type"])
    elif t in ("RESCHEDULE_IN", "RESCHEDULE_OUT", "CAPACITY_LEVEL", "DEFER"):
        fields = []
        if sug.suggested_receipt_date:
            po.planned_receipt_date = sug.suggested_receipt_date
            fields.append("planned_receipt_date")
        if sug.suggested_release_date:
            po.planned_release_date = sug.suggested_release_date
            fields.append("planned_release_date")
        if t in ("RESCHEDULE_IN", "RESCHEDULE_OUT"):
            po.action_type = t
            fields.append("action_type")
        if fields:
            po.save(update_fields=fields)
    else:
        raise RescheduleError("NOT_APPLICABLE", "Unknown suggestion type for a planned order.")


def _apply_work_order_operation(sug):
    op = sug.work_order_operation
    wo = op.work_order
    if wo.status not in ("PLANNED", "FIRM"):
        raise RescheduleError("UNSAFE", f"Work order is {wo.get_status_display()}; cannot reschedule it.")
    fields = []
    if sug.suggested_start:
        op.planned_start = sug.suggested_start
        fields.append("planned_start")
    if sug.suggested_end:
        op.planned_end = sug.suggested_end
        fields.append("planned_end")
    if fields:
        op.save(update_fields=fields)
    starts = [o.planned_start for o in wo.operations.all() if o.planned_start]
    ends = [o.planned_end for o in wo.operations.all() if o.planned_end]
    if starts and ends:
        wo.planned_start_date = min(starts)
        wo.planned_end_date = max(ends)
        wo.save(update_fields=["planned_start_date", "planned_end_date"])


@transaction.atomic
def reject_suggestion(sug, user, reason=None):
    if sug.status not in ("SUGGESTED", "ACCEPTED"):
        raise RescheduleError("ALREADY_RESOLVED", f"Suggestion is already {sug.get_status_display()}.")
    sug.status = "REJECTED"
    sug.rejected_by = user
    sug.rejected_at = timezone.now()
    sug.rejection_reason = (reason or "")[:255]
    sug.save(update_fields=["status", "rejected_by", "rejected_at", "rejection_reason"])
    return sug


def bulk_apply_suggestions(suggestions, user):
    result = {"applied": 0, "failed": 0, "reasons": []}
    for sug in suggestions:
        try:
            apply_suggestion(sug, user)
            result["applied"] += 1
        except RescheduleError as e:
            result["failed"] += 1
            result["reasons"].append(f"{sug.suggestion_number}: {e}")
    return result


def bulk_reject_suggestions(suggestions, user, reason=None):
    result = {"rejected": 0, "failed": 0}
    for sug in suggestions:
        try:
            reject_suggestion(sug, user, reason)
            result["rejected"] += 1
        except RescheduleError:
            result["failed"] += 1
    return result
