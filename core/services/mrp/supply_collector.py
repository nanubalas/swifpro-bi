"""Supply collection for MRP (Phase 2: on-hand, open POs, open PRs).

Per item-site planning profile, records:
- ON_HAND nettable availability (reuses inventory_snapshot, which reuses the
  existing balance tables).
- PURCHASE_ORDER open quantity from current, open PO lines (ordered - received).
- PURCHASE_REQUISITION open quantity from approved, not-yet-converted PRs.

No PO / PR / inventory schema is changed. Phase 2 deliberately excludes work
orders, transfer orders and in-transit (later phases).
"""
from decimal import Decimal

from django.core.exceptions import ValidationError

from core.services.mrp import exceptions as mrp_exc
from core.services.mrp import inventory_snapshot

ZERO = Decimal("0.00")

# PO statuses that represent live, expected supply (committed but not finished).
OPEN_PO_STATUSES = {
    "SUBMITTED", "APPROVAL_PENDING", "APPROVED", "SENT", "IN_TRANSIT", "PARTIALLY_RECEIVED",
}


def _to_base(product, qty, uom, run):
    from core.services.uom import to_base_qty
    try:
        return to_base_qty(product, qty, uom)
    except ValidationError:
        mrp_exc.raise_exception(
            run, "MISSING_UOM_CONVERSION",
            f"No UOM conversion for {product.sku}; quantity used as entered.",
            product=product, dedupe_key=("uom", product.id))
        return Decimal(qty or 0)


def _pr_attribution_site_id(run):
    """PRs carry no site, so their supply is attributed to the run's scoped site
    or the tenant default site."""
    if run.site_scope_id:
        return run.site_scope_id
    from core.models import Site
    s = (Site.objects.filter(tenant=run.tenant, is_default=True).order_by("id").first()
         or Site.objects.filter(tenant=run.tenant).order_by("id").first())
    return s.id if s else None


def collect(run, profile):
    """Create and return the list of MRPSupply rows for one planning profile."""
    from core.models import PurchaseOrderLine, PurchaseRequisitionLine, MRPSupply

    tenant = run.tenant
    product = profile.product
    site = profile.site
    start = run.planning_start_date
    created = []

    # --- On-hand (nettable) ---
    available, excluded, expired = inventory_snapshot.nettable_on_hand(
        tenant, product, site, as_of=start)
    if available > ZERO:
        created.append(MRPSupply.objects.create(
            mrp_run=run, tenant=tenant, product=product, site=site,
            supply_type="ON_HAND", source_document_type="InventoryBalance",
            receipt_date=start, quantity=available, available_quantity=available,
        ))
    if excluded > ZERO:
        mrp_exc.raise_exception(
            run, "NON_NETTABLE_STOCK",
            f"{excluded} of {product.sku} at {site.name} is in quarantine/damaged/transit "
            f"and was excluded from availability.",
            product=product, site=site, dedupe_key=("nonnet", product.id, site.id))
    if expired > ZERO:
        mrp_exc.raise_exception(
            run, "EXPIRED_LOT",
            f"{expired} of {product.sku} at {site.name} is in expired lots and was excluded.",
            product=product, site=site, dedupe_key=("expired", product.id, site.id))

    # --- Open purchase orders ---
    po_lines = (PurchaseOrderLine.objects
                .filter(po__tenant=tenant, po__is_current=True, po__status__in=OPEN_PO_STATUSES,
                        product=product, po__site=site)
                .select_related("po", "product", "uom"))
    for line in po_lines:
        open_qty = line.open_qty
        if open_qty is None or open_qty <= ZERO:
            continue
        po = line.po
        base_qty = _to_base(product, open_qty, line.uom, run)
        if base_qty <= ZERO:
            continue
        receipt = po.expected_date or start
        if po.expected_date is None:
            mrp_exc.raise_exception(
                run, "PURCHASE_ORDER_DUE_DATE_MISSING",
                f"Purchase order {po.po_number} has no expected date; used planning start {start}.",
                product=product, site=site,
                source_document_type="PurchaseOrder", source_document_id=po.po_number,
                dedupe_key=("po_date", po.id))
        elif po.expected_date < start:
            mrp_exc.raise_exception(
                run, "PAST_DUE_SUPPLY",
                f"Purchase order {po.po_number} for {product.sku} is overdue ({po.expected_date}).",
                product=product, site=site,
                source_document_type="PurchaseOrder", source_document_id=po.po_number,
                dedupe_key=("po_pastdue", line.id))
        created.append(MRPSupply.objects.create(
            mrp_run=run, tenant=tenant, product=product, site=site,
            supply_type="PURCHASE_ORDER",
            source_document_type="PurchaseOrder", source_document_id=po.po_number,
            source_line_id=str(line.id),
            receipt_date=receipt, quantity=base_qty, available_quantity=base_qty,
        ))

    # --- Open purchase requisitions (approved, not yet converted) ---
    if site.id == _pr_attribution_site_id(run):
        pr_lines = (PurchaseRequisitionLine.objects
                    .filter(requisition__tenant=tenant, requisition__status="APPROVED",
                            requisition__converted_po__isnull=True, product=product)
                    .select_related("requisition", "product"))
        for line in pr_lines:
            qty = line.quantity or ZERO
            if qty <= ZERO:
                continue
            req = line.requisition
            receipt = req.needed_by or start
            created.append(MRPSupply.objects.create(
                mrp_run=run, tenant=tenant, product=product, site=site,
                supply_type="PURCHASE_REQUISITION",
                source_document_type="PurchaseRequisition", source_document_id=req.req_number,
                source_line_id=str(line.id),
                receipt_date=receipt, quantity=qty, available_quantity=qty,
            ))

    # --- Inbound transfers to this site (open + in-transit) as destination supply ---
    if run.include_transfers:
        created.extend(_inbound_transfer_supply(run, product, site, start))

    return created


# Open transfer statuses: not yet finished (RECEIVED/POSTED already hit on-hand;
# CANCELLED is dead).
OPEN_TRANSFER_STATUSES = {"DRAFT", "DISPATCHED"}


def _inbound_transfer_supply(run, product, site, start):
    """MRPSupply rows for open transfers heading INTO ``site``. The not-yet-shipped
    portion (qty - dispatched) is TRANSFER_ORDER; the shipped-not-received portion
    (dispatched - received) is IN_TRANSIT - split this way so the two never
    double-count. Transfers carry no due date, so receipt is the planning start
    with an informational exception."""
    from core.models import InventoryTransferLine, MRPSupply

    tenant = run.tenant
    rows = []
    lines = (InventoryTransferLine.objects
             .filter(transfer__tenant=tenant, transfer__to_location__site=site,
                     transfer__status__in=OPEN_TRANSFER_STATUSES, product=product)
             .select_related("transfer"))
    for line in lines:
        t = line.transfer
        on_order = (line.qty or ZERO) - (line.dispatched_qty or ZERO)
        in_transit = (line.dispatched_qty or ZERO) - (line.received_qty or ZERO)
        if on_order <= ZERO and in_transit <= ZERO:
            continue
        mrp_exc.raise_exception(
            run, "INBOUND_TRANSFER_DUE_DATE_MISSING",
            f"Transfer {t.transfer_number} into {site.name} has no due date; used planning start {start}.",
            product=product, site=site,
            source_document_type="InventoryTransfer", source_document_id=t.transfer_number,
            dedupe_key=("xfer_date", t.id))
        if on_order > ZERO:
            rows.append(MRPSupply.objects.create(
                mrp_run=run, tenant=tenant, product=product, site=site,
                supply_type="TRANSFER_ORDER",
                source_document_type="InventoryTransfer", source_document_id=t.transfer_number,
                source_line_id=str(line.id),
                receipt_date=start, quantity=on_order, available_quantity=on_order))
        if in_transit > ZERO:
            rows.append(MRPSupply.objects.create(
                mrp_run=run, tenant=tenant, product=product, site=site,
                supply_type="IN_TRANSIT",
                source_document_type="InventoryTransfer", source_document_id=t.transfer_number,
                source_line_id=str(line.id),
                receipt_date=start, quantity=in_transit, available_quantity=in_transit))
    return rows
