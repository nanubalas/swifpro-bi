"""Replenishment planning: turn balances + policy into suggested actions.

Pure planning/advisory - it reads the inventory ledger, open POs and in-transit
transfers and proposes purchase / transfer quantities. It never posts movements,
costing or GL. The core formula:

    Projected Available = On Hand + Open Inbound PO + In-Transit Inbound - Reserved

A line needs replenishment when Projected Available < reorder point; the
suggested quantity refills to max_stock (else reorder_quantity, else reorder
point), then is bumped to the supplier MOQ and rounded up to the pack size.
"""
from decimal import Decimal, ROUND_CEILING
from django.db.models import Sum, F

from core.models import (
    Product, Location, InventoryBalance, ReplenishmentPolicy,
    PurchaseOrderLine, PurchaseOrder, InventoryTransfer, InventoryTransferLine,
    PurchaseRequisition, PurchaseRequisitionLine,
)

ZERO = Decimal("0.00")

# PO statuses that represent stock genuinely on its way in.
OPEN_PO_STATUSES = [
    PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.SENT,
    PurchaseOrder.Status.IN_TRANSIT, PurchaseOrder.Status.PARTIALLY_RECEIVED,
]

# Urgency ranking (highest first) for sorting/filtering.
STATUS_ORDER = {"critical": 0, "below_safety": 1, "below_reorder": 2, "overstock": 3, "okay": 4}


class _Policy:
    """Resolved, effective replenishment settings for a (product, location)."""
    __slots__ = ("min_stock", "max_stock", "safety_stock", "reorder_point", "reorder_quantity",
                 "eoq", "lead_time_days", "preferred_supplier", "moq", "pack_size", "is_active", "source")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def resolve_policy(product, location, *, loc_policy=None, prod_policy=None):
    """Effective settings: a location override wins as a whole row, else the
    product-level default, else fall back to Product.reorder_level /
    preferred_supplier. Pass prefetched policies to avoid per-row queries."""
    pol = loc_policy or prod_policy
    if pol is not None:
        return _Policy(
            min_stock=pol.min_stock, max_stock=pol.max_stock, safety_stock=pol.safety_stock,
            reorder_point=pol.reorder_point or product.reorder_level or ZERO,
            reorder_quantity=pol.reorder_quantity, eoq=pol.eoq,
            lead_time_days=pol.lead_time_days or 0,
            preferred_supplier=pol.preferred_supplier or product.preferred_supplier,
            moq=pol.moq or ZERO, pack_size=pol.pack_size or ZERO,
            is_active=pol.is_active, source=("location" if loc_policy else "product"))
    return _Policy(
        min_stock=ZERO, max_stock=ZERO, safety_stock=ZERO,
        reorder_point=product.reorder_level or ZERO, reorder_quantity=ZERO, eoq=None,
        lead_time_days=0, preferred_supplier=product.preferred_supplier,
        moq=ZERO, pack_size=ZERO, is_active=True, source="default")


def _round_order_qty(need, moq, pack_size):
    """Bump a raw need up to the MOQ, then round up to the pack-size multiple."""
    qty = max(need, moq or ZERO)
    if qty <= ZERO:
        return ZERO
    if pack_size and pack_size > ZERO:
        packs = (qty / pack_size).to_integral_value(rounding=ROUND_CEILING)
        qty = packs * pack_size
    return qty.quantize(Decimal("0.01"))


def abc_classes(tenant):
    """Map product_id -> 'A'/'B'/'C' by on-hand inventory value (A=top 80%,
    B=next 15%, C=rest). Zero-value products are C."""
    rows = (InventoryBalance.objects.filter(tenant=tenant)
            .values("product_id").annotate(qty=Sum("on_hand")))
    costs = {p.id: (p.average_cost or p.standard_cost or ZERO)
             for p in Product.objects.filter(tenant=tenant)}
    values = []
    for r in rows:
        v = (r["qty"] or ZERO) * costs.get(r["product_id"], ZERO)
        if v > ZERO:
            values.append((r["product_id"], v))
    values.sort(key=lambda x: x[1], reverse=True)
    total = sum(v for _, v in values) or ZERO
    out = {}
    cum = ZERO
    for pid, v in values:
        cum += v
        share = (cum / total) if total else Decimal("1")
        out[pid] = "A" if share <= Decimal("0.80") else ("B" if share <= Decimal("0.95") else "C")
    return out


def _site_single_location(tenant):
    """site_id -> the id of its ONLY active stock-holding location (sites with
    zero or several stock locations are omitted). Lets us safely infer the
    receiving location of a PO that left it blank, without guessing."""
    from collections import defaultdict
    by_site = defaultdict(list)
    for l in (Location.objects.filter(tenant=tenant, is_active=True, holds_stock=True)
              .values("id", "site_id")):
        if l["site_id"]:
            by_site[l["site_id"]].append(l["id"])
    return {sid: ids[0] for sid, ids in by_site.items() if len(ids) == 1}


def _open_po_map(tenant):
    """Open inbound PO qty keyed by (product, location). A PO with a blank
    receiving location is attributed to its site's single stock location when one
    can be inferred; otherwise it is left out of projected availability and
    surfaced via excluded_inbound_po()."""
    single = _site_single_location(tenant)
    rows = (PurchaseOrderLine.objects
            .filter(po__tenant=tenant, po__is_current=True, po__status__in=OPEN_PO_STATUSES)
            .values("product_id", "po__receiving_location_id", "po__site_id")
            .annotate(q=Sum(F("ordered_qty") - F("received_qty"))))
    m = {}
    for r in rows:
        q = r["q"] or ZERO
        if q <= ZERO:
            continue
        loc_id = r["po__receiving_location_id"] or single.get(r["po__site_id"])
        if loc_id is None:
            continue  # unattributable -> reported by excluded_inbound_po()
        key = (r["product_id"], loc_id)
        m[key] = m.get(key, ZERO) + q
    return m


def excluded_inbound_po(tenant):
    """Open PO inbound that cannot be placed on the plan because the receiving
    location is missing and not safely inferable. Returns {count, qty, lines}
    so the UI can warn instead of silently dropping the stock."""
    single = _site_single_location(tenant)
    lines = (PurchaseOrderLine.objects
             .filter(po__tenant=tenant, po__is_current=True, po__status__in=OPEN_PO_STATUSES,
                     po__receiving_location__isnull=True)
             .select_related("po", "product"))
    out, total = [], ZERO
    for l in lines:
        if single.get(l.po.site_id):
            continue  # safely inferred -> already counted in the plan
        open_qty = (l.ordered_qty or ZERO) - (l.received_qty or ZERO)
        if open_qty <= ZERO:
            continue
        total += open_qty
        if len(out) < 100:
            out.append({"po_number": l.po.po_number, "sku": l.product.sku,
                        "product": l.product.name, "open_qty": open_qty})
    return {"count": len(out), "qty": total, "lines": out}


def _in_transit_map(tenant):
    out = {}
    lines = (InventoryTransferLine.objects
             .filter(transfer__tenant=tenant, transfer__status=InventoryTransfer.Status.DISPATCHED)
             .select_related("transfer"))
    for l in lines:
        key = (l.product_id, l.transfer.to_location_id)
        out[key] = out.get(key, ZERO) + l.in_transit_qty
    return out


def _status(on_hand, projected, pol):
    if on_hand <= ZERO and (pol.reorder_point > ZERO or pol.safety_stock > ZERO):
        return "critical"
    if pol.safety_stock > ZERO and projected <= pol.safety_stock:
        return "below_safety"
    if pol.reorder_point > ZERO and projected < pol.reorder_point:
        return "below_reorder"
    if pol.max_stock > ZERO and on_hand > pol.max_stock:
        return "overstock"
    return "okay"


def _suggested_purchase(projected, pol):
    if not (pol.reorder_point > ZERO and projected < pol.reorder_point):
        return ZERO
    if pol.max_stock > ZERO:
        need = pol.max_stock - projected
    elif pol.reorder_quantity > ZERO:
        need = pol.reorder_quantity
    else:
        need = pol.reorder_point - projected
    if need <= ZERO:
        return ZERO
    return _round_order_qty(need, pol.moq, pol.pack_size)


def plan(tenant, *, location=None, supplier=None, category=None, status=None,
         below_reorder=False, overstock=False, active_only=True):
    """Build the replenishment plan: a list of row dicts (one per product/location
    with stock or a policy). Filters are applied after computation."""
    abc = abc_classes(tenant)
    open_po = _open_po_map(tenant)
    in_transit = _in_transit_map(tenant)

    # Policies, keyed for cheap lookup.
    loc_pol, prod_pol = {}, {}
    for pol in ReplenishmentPolicy.objects.filter(tenant=tenant).select_related("preferred_supplier"):
        if pol.location_id:
            loc_pol[(pol.product_id, pol.location_id)] = pol
        else:
            prod_pol[pol.product_id] = pol

    # Candidate (product, location) pairs: every balance row, plus any
    # location-specific policy that has no balance yet.
    balances = {}
    bq = InventoryBalance.objects.filter(tenant=tenant).select_related("product", "location")
    if active_only:
        bq = bq.filter(product__is_active=True)
    for b in bq:
        balances[(b.product_id, b.location_id)] = b
    products = {p.id: p for p in Product.objects.filter(tenant=tenant).select_related("preferred_supplier")}
    locations = {l.id: l for l in Location.objects.filter(tenant=tenant)}

    keys = set(balances.keys())
    for (pid, lid) in loc_pol.keys():
        keys.add((pid, lid))

    # Excess by (product, location) for transfer suggestions: on_hand above max.
    excess = {}
    for (pid, lid), b in balances.items():
        lp = resolve_policy(products[b.product_id], locations.get(lid),
                            loc_policy=loc_pol.get((pid, lid)), prod_policy=prod_pol.get(pid))
        if lp.max_stock > ZERO:
            over = (b.on_hand or ZERO) - lp.max_stock
            if over > ZERO:
                excess.setdefault(pid, []).append((lid, over))

    rows = []
    for (pid, lid) in keys:
        product = products.get(pid)
        loc = locations.get(lid)
        if product is None or loc is None:
            continue
        if active_only and not product.is_active:
            continue
        pol = resolve_policy(product, loc, loc_policy=loc_pol.get((pid, lid)), prod_policy=prod_pol.get(pid))
        if active_only and not pol.is_active:
            continue
        b = balances.get((pid, lid))
        on_hand = (b.on_hand if b else ZERO) or ZERO
        reserved = (b.reserved if b else ZERO) or ZERO
        available = on_hand - reserved
        inbound = open_po.get((pid, lid), ZERO)
        transit = in_transit.get((pid, lid), ZERO)
        projected = on_hand + inbound + transit - reserved

        st = _status(on_hand, projected, pol)
        suggested_po = _suggested_purchase(projected, pol)

        # Transfer suggestion: if short, can another location cover it from excess?
        suggested_transfer = ZERO
        transfer_from = None
        if suggested_po > ZERO:
            others = [(olid, ov) for (olid, ov) in excess.get(pid, []) if olid != lid]
            if others:
                olid, ov = max(others, key=lambda x: x[1])
                suggested_transfer = min(suggested_po, ov)
                transfer_from = locations.get(olid)

        # Estimated days of cover (rough): derive a daily demand from the policy
        # (consumption over the lead time down to safety stock) when available.
        days_cover = None
        if pol.lead_time_days and pol.reorder_point > ZERO:
            daily = (pol.reorder_point - pol.safety_stock) / Decimal(pol.lead_time_days)
            if daily > ZERO:
                days_cover = int((available / daily)) if available > ZERO else 0

        # Skip rows with no policy signal at all and healthy stock (noise).
        if pol.source == "default" and pol.reorder_point <= ZERO and on_hand <= ZERO:
            continue

        rows.append({
            "product": product, "location": loc, "abc": abc.get(pid, "C"),
            "on_hand": on_hand, "reserved": reserved, "available": available,
            "inbound_po": inbound, "in_transit": transit, "projected_available": projected,
            "safety_stock": pol.safety_stock, "reorder_point": pol.reorder_point,
            "min_stock": pol.min_stock, "max_stock": pol.max_stock, "eoq": pol.eoq,
            "suggested_purchase_qty": suggested_po, "suggested_transfer_qty": suggested_transfer,
            "transfer_from": transfer_from, "preferred_supplier": pol.preferred_supplier,
            "status": st, "days_cover": days_cover, "policy_source": pol.source,
        })

    # Filters.
    if location is not None:
        rows = [r for r in rows if r["location"].id == getattr(location, "id", location)]
    if supplier is not None:
        sid = getattr(supplier, "id", supplier)
        rows = [r for r in rows if r["preferred_supplier"] and r["preferred_supplier"].id == sid]
    if category is not None:
        cid = getattr(category, "id", category)
        rows = [r for r in rows if r["product"].category_id == cid]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if below_reorder:
        rows = [r for r in rows if r["status"] in ("critical", "below_safety", "below_reorder")]
    if overstock:
        rows = [r for r in rows if r["status"] == "overstock"]

    rows.sort(key=lambda r: (STATUS_ORDER.get(r["status"], 9), -float(r["suggested_purchase_qty"])))
    return rows


def open_requisition_product_ids(tenant):
    """Product IDs that already sit on an open (draft/submitted/approved)
    requisition - used to avoid raising duplicate replenishment requisitions."""
    open_statuses = [PurchaseRequisition.Status.DRAFT, PurchaseRequisition.Status.SUBMITTED,
                     PurchaseRequisition.Status.APPROVED]
    return set(PurchaseRequisitionLine.objects
               .filter(requisition__tenant=tenant, requisition__status__in=open_statuses)
               .values_list("product_id", flat=True))
