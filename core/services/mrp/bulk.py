"""Bulk / grouped planner actions for the MRP workbench (Phase 8).

Status changes (firm / ignore / cancel) and conversions can be applied to many
planned orders at once. Conversions are *grouped where safe*:

- BUY      -> one Purchase Requisition per (supplier, destination site), with one
              PR line per planned order (each line links back to its planned
              order). A PURCHASE_ORDER target falls back to one PO per order.
- TRANSFER -> one Transfer Order per (source site, destination site); planned
              orders for the same product are summed into a single transfer line
              to respect the (transfer, product, lot, serial, expiry) uniqueness.
- MAKE     -> one Work Order per planned order. Work orders are single-product,
              so grouping is not safe; we fall back to per-order conversion.

Everything is processed defensively: one invalid order never aborts the batch.
Already-converted / ignored / cancelled / expired orders are skipped with a
reason. Re-running a bulk conversion never duplicates documents - converted
orders leave the convertible set (status becomes CONVERTED) and each order's
existing-document link is re-checked before anything is created. Each group /
order runs in its own transaction, so a failing group rolls back alone. Nothing
here posts inventory, GL or WIP.
"""
from collections import defaultdict

from django.db import transaction

from core.services.mrp import conversion
from core.services.mrp.conversion import (
    CONVERTIBLE_STATUSES, ZERO, ConversionError,
    _default_location_for_site, _mark_converted, _unique_number,
)

STATUS_ACTIONS = {"firm": "FIRMED", "ignore": "IGNORED", "cancel": "CANCELLED"}
_CHANGEABLE = {"SUGGESTED", "FIRMED"}


class BulkResult:
    """Accumulates the outcome of a bulk action for user-facing messaging."""

    def __init__(self):
        self.success = 0
        self.skipped = 0
        self.failed = 0
        self.documents = []   # (kind, url_name, object_id, number)
        self.reasons = []     # human-facing strings for skipped / failed orders

    def ok(self, n=1):
        self.success += n

    def skip(self, po, reason):
        self.skipped += 1
        self.reasons.append(f"{po.planned_order_number}: skipped - {reason}")

    def fail(self, po, reason):
        self.failed += 1
        self.reasons.append(f"{po.planned_order_number}: failed - {reason}")

    def document(self, kind, url_name, obj_id, number):
        self.documents.append((kind, url_name, obj_id, number))

    @property
    def summary(self):
        return f"{self.success} succeeded, {self.skipped} skipped, {self.failed} failed."


# --------------------------------------------------------------------------- #
# Status actions (firm / ignore / cancel)
# --------------------------------------------------------------------------- #
def bulk_status_action(planned_orders, action, user):
    """Apply firm / ignore / cancel to many planned orders. Only SUGGESTED or
    FIRMED orders change; the rest are skipped with a reason."""
    result = BulkResult()
    new_status = STATUS_ACTIONS.get(action)
    if new_status is None:
        return result
    for po in planned_orders:
        if po.status not in _CHANGEABLE:
            result.skip(po, f"is {po.get_status_display()}")
            continue
        po.status = new_status
        po.save(update_fields=["status"])
        result.ok()
    return result


# --------------------------------------------------------------------------- #
# Conversion dispatcher
# --------------------------------------------------------------------------- #
def bulk_convert(planned_orders, user, buy_target=conversion.REQUISITION):
    """Convert many planned orders, grouping BUY/TRANSFER where safe."""
    result = BulkResult()
    buckets = defaultdict(list)
    for po in planned_orders:
        buckets[po.source_type].append(po)

    if buckets.get("BUY"):
        if buy_target == conversion.PURCHASE_ORDER:
            _convert_buy_to_purchase_orders(buckets["BUY"], user, result)
        else:
            _group_buy_to_requisitions(buckets["BUY"], user, result)
    if buckets.get("TRANSFER"):
        _group_transfer_to_orders(buckets["TRANSFER"], user, result)
    if buckets.get("MAKE"):
        _convert_make_to_work_orders(buckets["MAKE"], user, result)
    if buckets.get("SUBCONTRACT"):
        if buy_target == conversion.PURCHASE_ORDER:
            _convert_subcontract_to_purchase_orders(buckets["SUBCONTRACT"], user, result)
        else:
            _group_buy_to_requisitions(buckets["SUBCONTRACT"], user, result, subcontract=True)
    return result


def _eligible(po, result, already_field):
    """Shared pre-checks. Returns True if the order should still be processed."""
    if getattr(po, already_field):
        result.skip(po, "already has a linked document")
        return False
    if po.status not in CONVERTIBLE_STATUSES:
        result.skip(po, f"is {po.get_status_display()}")
        return False
    if (po.quantity or ZERO) <= ZERO:
        result.fail(po, "quantity must be greater than zero")
        return False
    return True


# --------------------------------------------------------------------------- #
# BUY -> grouped Purchase Requisitions
# --------------------------------------------------------------------------- #
def _group_buy_to_requisitions(orders, user, result, subcontract=False):
    from core.models import PurchaseRequisition, PurchaseRequisitionLine

    prefix = "SCR" if subcontract else "PR"
    groups = defaultdict(list)
    for po in orders:
        if not _eligible(po, result, "created_purchase_requisition_id"):
            continue
        if po.supplier_id is None:
            result.fail(po, "no subcontract supplier set" if subcontract else "no supplier set")
            continue
        groups[(po.supplier_id, po.site_id)].append(po)

    for gos in groups.values():
        head = gos[0]
        kind = "Subcontract service" if subcontract else "From MRP run"
        try:
            with transaction.atomic():
                req = PurchaseRequisition.objects.create(
                    tenant=head.tenant,
                    req_number=_unique_number(PurchaseRequisition, "req_number", prefix, head.tenant),
                    preferred_supplier=head.supplier,
                    needed_by=min(o.required_date for o in gos),
                    justification=(f"{kind} {head.mrp_run.run_number} "
                                   f"({len(gos)} planned order(s))."),
                    status="DRAFT", requested_by=user)
                for o in gos:
                    note = (f"Subcontract service - MRP {o.planned_order_number}" if subcontract
                            else f"MRP {o.planned_order_number}")
                    PurchaseRequisitionLine.objects.create(
                        requisition=req, product=o.product, quantity=o.quantity,
                        estimated_unit_cost=(_sub_unit_cost(o) if subcontract
                                             else (o.product.standard_cost or None)),
                        notes=note, mrp_planned_order=o)
                    _mark_converted(o, user, created_purchase_requisition=req)
            result.document("Requisition", "requisition_detail", req.id, req.req_number)
            result.ok(len(gos))
        except Exception as e:  # pragma: no cover - defensive; group rolls back alone
            for o in gos:
                result.fail(o, str(e))


def _sub_unit_cost(po):
    from core.services.mrp.conversion import _subcontract_unit_cost
    return _subcontract_unit_cost(po)


def _convert_subcontract_to_purchase_orders(orders, user, result):
    for po in orders:
        if not _eligible(po, result, "created_purchase_order_id"):
            continue
        try:
            with transaction.atomic():
                doc, created, _ = conversion.convert_subcontract_to_purchase_order(po, user)
            if created:
                result.document("Purchase order", "po_detail", doc.id, doc.po_number)
                result.ok()
            else:
                result.skip(po, "already converted")
        except ConversionError as e:
            result.fail(po, str(e))


# --------------------------------------------------------------------------- #
# BUY -> individual draft Purchase Orders (no safe grouping by header)
# --------------------------------------------------------------------------- #
def _convert_buy_to_purchase_orders(orders, user, result):
    for po in orders:
        if not _eligible(po, result, "created_purchase_order_id"):
            continue
        try:
            with transaction.atomic():
                doc, created, _ = conversion.convert_buy_to_purchase_order(po, user)
            if created:
                result.document("Purchase order", "po_detail", doc.id, doc.po_number)
                result.ok()
            else:
                result.skip(po, "already converted")
        except ConversionError as e:
            result.fail(po, str(e))


# --------------------------------------------------------------------------- #
# TRANSFER -> grouped Transfer Orders (DRAFT, no movement)
# --------------------------------------------------------------------------- #
def _group_transfer_to_orders(orders, user, result):
    from core.models import InventoryTransfer, InventoryTransferLine

    groups = defaultdict(list)
    for po in orders:
        if not _eligible(po, result, "created_transfer_order_id"):
            continue
        if po.transfer_from_site_id is None:
            result.fail(po, "no transfer source site")
            continue
        if po.transfer_from_site_id == po.site_id:
            result.fail(po, "source site equals destination site")
            continue
        groups[(po.transfer_from_site_id, po.site_id)].append(po)

    for gos in groups.values():
        head = gos[0]
        from_loc = _default_location_for_site(head.tenant, head.transfer_from_site)
        to_loc = _default_location_for_site(head.tenant, head.site)
        if from_loc is None or to_loc is None:
            for o in gos:
                result.fail(o, "source and destination sites both need an active stock location")
            continue
        try:
            with transaction.atomic():
                transfer = InventoryTransfer.objects.create(
                    tenant=head.tenant,
                    transfer_number=_unique_number(InventoryTransfer, "transfer_number", "TR", head.tenant),
                    from_location=from_loc, to_location=to_loc, status="DRAFT",
                    notes=(f"From MRP run {head.mrp_run.run_number} "
                           f"({len(gos)} planned order(s))."))
                # Sum quantities per product so one product never produces two
                # lines (the transfer line uniqueness would reject the second).
                qty_by_product = defaultdict(lambda: ZERO)
                product_by_id = {}
                for o in gos:
                    qty_by_product[o.product_id] += o.quantity
                    product_by_id[o.product_id] = o.product
                for pid, qty in qty_by_product.items():
                    InventoryTransferLine.objects.create(
                        transfer=transfer, product=product_by_id[pid], qty=qty)
                for o in gos:
                    _mark_converted(o, user, created_transfer_order=transfer)
            result.document("Transfer order", "transfer_detail", transfer.id, transfer.transfer_number)
            result.ok(len(gos))
        except Exception as e:  # pragma: no cover - defensive; group rolls back alone
            for o in gos:
                result.fail(o, str(e))


# --------------------------------------------------------------------------- #
# MAKE -> individual Work Orders (single-product; no safe grouping)
# --------------------------------------------------------------------------- #
def _convert_make_to_work_orders(orders, user, result):
    for po in orders:
        if not _eligible(po, result, "created_work_order_id"):
            continue
        try:
            with transaction.atomic():
                doc, created, _ = conversion.convert_make_to_work_order(po, user)
            if created:
                result.document("Work order", "work_order_detail", doc.id, doc.work_order_number)
                result.ok()
            else:
                result.skip(po, "already converted")
        except ConversionError as e:
            result.fail(po, str(e))
