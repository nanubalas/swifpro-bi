"""Convert MRP planned orders into real ERP documents (Phase 5).

- BUY      -> Purchase Requisition (default) or a DRAFT Purchase Order
- TRANSFER -> a DRAFT Transfer Order (no stock movement)
- MAKE     -> a PLANNED Work Order with material lines from component demand

All conversions are idempotent: if a planned order already has its target
document, the existing one is returned and nothing new is created. Only
SUGGESTED or FIRMED orders convert. Nothing here posts inventory, GL, WIP, or
issues/reserves material - documents are created for later execution. Reuses the
existing PurchaseRequisition / PurchaseOrder / InventoryTransfer models and the
Phase 5 WorkOrder models.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.crypto import get_random_string

ZERO = Decimal("0.00")

CONVERTIBLE_STATUSES = {"SUGGESTED", "FIRMED"}

# Target types accepted by convert_planned_order.
REQUISITION = "REQUISITION"
PURCHASE_ORDER = "PURCHASE_ORDER"
TRANSFER_ORDER = "TRANSFER_ORDER"
WORK_ORDER = "WORK_ORDER"

# Error / message codes (surfaced to the user; not stored as MRPExceptions).
ALREADY_CONVERTED = "PLANNED_ORDER_ALREADY_CONVERTED"
NOT_CONVERTIBLE = "PLANNED_ORDER_NOT_CONVERTIBLE"
MISSING_SUPPLIER = "CONVERSION_MISSING_SUPPLIER"
MISSING_TRANSFER_SOURCE = "CONVERSION_MISSING_TRANSFER_SOURCE"
INVALID_TRANSFER_SOURCE = "CONVERSION_INVALID_TRANSFER_SOURCE"
INVALID_QUANTITY = "CONVERSION_INVALID_QUANTITY"
MISSING_DEFAULT_LOCATION = "CONVERSION_MISSING_DEFAULT_LOCATION"
WRONG_TARGET = "CONVERSION_WRONG_TARGET"
MATERIALS_MISSING = "WORK_ORDER_MATERIALS_MISSING"


class ConversionError(Exception):
    """Raised when a planned order cannot be converted. ``code`` is one of the
    constants above; ``str(err)`` is a user-facing message."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _stamp():
    return timezone.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + get_random_string(4).upper()


def _unique_number(model, field, prefix, tenant):
    while True:
        candidate = f"{prefix}-{_stamp()}"
        if not model.objects.filter(tenant=tenant, **{field: candidate}).exists():
            return candidate


def _default_location_for_site(tenant, site):
    """A stock-holding location to act as a site's default for transfers."""
    from core.models import Location
    return (Location.objects.filter(tenant=tenant, site=site, is_active=True, holds_stock=True)
            .order_by("id").first())


def _mark_converted(planned_order, user, **linked):
    for field, value in linked.items():
        setattr(planned_order, field, value)
    planned_order.status = "CONVERTED"
    planned_order.converted_by = user
    planned_order.converted_at = timezone.now()
    planned_order.save(update_fields=list(linked.keys()) + ["status", "converted_by", "converted_at"])


def _guard_status(planned_order):
    if planned_order.status not in CONVERTIBLE_STATUSES:
        raise ConversionError(
            NOT_CONVERTIBLE,
            f"Planned order {planned_order.planned_order_number} is {planned_order.get_status_display()} "
            f"and cannot be converted.")


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #
def convert_planned_order(planned_order, target_type, user):
    """Convert ``planned_order`` to ``target_type``. Returns
    ``(document, created, message)``. Raises ConversionError on validation
    failure."""
    handlers = {
        REQUISITION: convert_buy_to_requisition,
        PURCHASE_ORDER: convert_buy_to_purchase_order,
        TRANSFER_ORDER: convert_transfer_to_transfer_order,
        WORK_ORDER: convert_make_to_work_order,
    }
    handler = handlers.get(target_type)
    if handler is None:
        raise ConversionError(WRONG_TARGET, f"Unknown conversion target '{target_type}'.")
    return handler(planned_order, user)


# --------------------------------------------------------------------------- #
# BUY -> Purchase Requisition
# --------------------------------------------------------------------------- #
def convert_buy_to_requisition(po, user):
    from core.models import PurchaseRequisition, PurchaseRequisitionLine

    if po.source_type != "BUY":
        raise ConversionError(WRONG_TARGET, "Only BUY planned orders convert to a requisition.")
    if po.created_purchase_requisition_id:
        return po.created_purchase_requisition, False, "Requisition already created for this planned order."
    _guard_status(po)
    if po.supplier_id is None:
        raise ConversionError(MISSING_SUPPLIER, "Set a supplier on the planned order before converting to a requisition.")
    if (po.quantity or ZERO) <= ZERO:
        raise ConversionError(INVALID_QUANTITY, "Planned order quantity must be greater than zero.")

    with transaction.atomic():
        req = PurchaseRequisition.objects.create(
            tenant=po.tenant,
            req_number=_unique_number(PurchaseRequisition, "req_number", "PR", po.tenant),
            preferred_supplier=po.supplier, needed_by=po.required_date,
            justification=f"From MRP planned order {po.planned_order_number}.",
            status="DRAFT", requested_by=user)
        PurchaseRequisitionLine.objects.create(
            requisition=req, product=po.product, quantity=po.quantity,
            estimated_unit_cost=(po.product.standard_cost or None),
            notes=f"MRP {po.planned_order_number}", mrp_planned_order=po)
        _mark_converted(po, user, created_purchase_requisition=req)
    return req, True, f"Requisition {req.req_number} created."


# --------------------------------------------------------------------------- #
# BUY -> Purchase Order (DRAFT only; never auto-submitted/approved)
# --------------------------------------------------------------------------- #
def _buy_unit_cost(po):
    from core.services.purchasing import last_prices_for_supplier
    try:
        prices = last_prices_for_supplier(po.tenant, po.supplier)
        if po.product_id in prices and prices[po.product_id]:
            return Decimal(prices[po.product_id])
    except Exception:
        pass
    return Decimal(po.product.standard_cost or ZERO)


def convert_buy_to_purchase_order(po, user):
    from core.models import PurchaseOrder, PurchaseOrderLine

    if po.source_type != "BUY":
        raise ConversionError(WRONG_TARGET, "Only BUY planned orders convert to a purchase order.")
    if po.created_purchase_order_id:
        return po.created_purchase_order, False, "Purchase order already created for this planned order."
    _guard_status(po)
    if po.supplier_id is None:
        raise ConversionError(MISSING_SUPPLIER, "Set a supplier on the planned order before converting to a PO.")
    if (po.quantity or ZERO) <= ZERO:
        raise ConversionError(INVALID_QUANTITY, "Planned order quantity must be greater than zero.")

    with transaction.atomic():
        order = PurchaseOrder.objects.create(
            tenant=po.tenant,
            po_number=_unique_number(PurchaseOrder, "po_number", "PO", po.tenant),
            supplier=po.supplier, site=po.site, status="DRAFT",
            expected_date=po.required_date,
            notes=f"From MRP planned order {po.planned_order_number}.")
        PurchaseOrderLine.objects.create(
            po=order, product=po.product, ordered_qty=po.quantity, unit_cost=_buy_unit_cost(po))
        _mark_converted(po, user, created_purchase_order=order)
    return order, True, f"Draft purchase order {order.po_number} created."


# --------------------------------------------------------------------------- #
# TRANSFER -> Transfer Order (DRAFT; no movement)
# --------------------------------------------------------------------------- #
def convert_transfer_to_transfer_order(po, user):
    from core.models import InventoryTransfer, InventoryTransferLine

    if po.source_type != "TRANSFER":
        raise ConversionError(WRONG_TARGET, "Only TRANSFER planned orders convert to a transfer order.")
    if po.created_transfer_order_id:
        return po.created_transfer_order, False, "Transfer order already created for this planned order."
    _guard_status(po)
    if po.transfer_from_site_id is None:
        raise ConversionError(MISSING_TRANSFER_SOURCE, "Planned order has no transfer source site.")
    if po.transfer_from_site_id == po.site_id:
        raise ConversionError(INVALID_TRANSFER_SOURCE, "Transfer source site equals the destination site.")
    if (po.quantity or ZERO) <= ZERO:
        raise ConversionError(INVALID_QUANTITY, "Planned order quantity must be greater than zero.")

    from_loc = _default_location_for_site(po.tenant, po.transfer_from_site)
    to_loc = _default_location_for_site(po.tenant, po.site)
    if from_loc is None or to_loc is None:
        raise ConversionError(
            MISSING_DEFAULT_LOCATION,
            "Both the source and destination sites need an active stock location for a transfer.")

    with transaction.atomic():
        transfer = InventoryTransfer.objects.create(
            tenant=po.tenant,
            transfer_number=_unique_number(InventoryTransfer, "transfer_number", "TR", po.tenant),
            from_location=from_loc, to_location=to_loc, status="DRAFT",
            notes=f"From MRP planned order {po.planned_order_number}.")
        InventoryTransferLine.objects.create(transfer=transfer, product=po.product, qty=po.quantity)
        _mark_converted(po, user, created_transfer_order=transfer)
    return transfer, True, f"Transfer order {transfer.transfer_number} created."


# --------------------------------------------------------------------------- #
# MAKE -> Work Order (PLANNED; material lines from component demand)
# --------------------------------------------------------------------------- #
def convert_make_to_work_order(po, user):
    from core.models import WorkOrder, WorkOrderMaterial, MRPDemand, BillOfMaterialsLine

    if po.source_type != "MAKE":
        raise ConversionError(WRONG_TARGET, "Only MAKE planned orders convert to a work order.")
    if po.created_work_order_id:
        return po.created_work_order, False, "Work order already created for this planned order."
    _guard_status(po)
    if (po.quantity or ZERO) <= ZERO:
        raise ConversionError(INVALID_QUANTITY, "Planned order quantity must be greater than zero.")

    component_demands = list(MRPDemand.objects.filter(
        mrp_run=po.mrp_run, demand_type="WORK_ORDER_COMPONENT",
        source_document_type="MRPPlannedOrder", source_document_id=po.planned_order_number)
        .select_related("product"))

    with transaction.atomic():
        wo = WorkOrder.objects.create(
            tenant=po.tenant,
            work_order_number=_unique_number(WorkOrder, "work_order_number", "WO", po.tenant),
            site=po.site, product=po.product, quantity=po.quantity, status="PLANNED",
            required_date=po.required_date, planned_start_date=po.planned_release_date,
            planned_end_date=po.planned_receipt_date, source_mrp_planned_order=po,
            created_by=user, notes=f"From MRP planned order {po.planned_order_number}.")
        for d in component_demands:
            bom_line = None
            if d.source_line_id and d.source_line_id.isdigit():
                bom_line = BillOfMaterialsLine.objects.filter(id=int(d.source_line_id)).first()
            WorkOrderMaterial.objects.create(
                work_order=wo, component=d.product, required_quantity=d.open_quantity,
                required_date=d.required_date, bom_line=bom_line, source_mrp_demand=d)
        _mark_converted(po, user, created_work_order=wo)

    if not component_demands:
        return wo, True, (f"Work order {wo.work_order_number} created with no material lines "
                          f"(no component demand found - {MATERIALS_MISSING}).")
    return wo, True, f"Work order {wo.work_order_number} created with {len(component_demands)} material line(s)."
