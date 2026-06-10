"""Legacy serial-data readiness audit.

The serial cardinality guard (apply_movement) is strict for new data, but stock
that predates it may violate the rules. This module DETECTS such legacy data so
it can be corrected before go-live. It is read-only — it never invents serials,
never mutates balances/financials and never rewrites movements. Optional "apply"
(in the command) only writes audit-log flags for traceability.

Detected issue types (severity):
  ONHAND_GT_1            (high)   a serial with on_hand > 1
  NEGATIVE_SERIAL_BALANCE(high)   a serial/lot row with on_hand < 0
  BLANK_SERIAL_BALANCE   (high)   on-hand stock of a serial product with no serial
  DUPLICATE_SERIAL       (high)   the same serial on hand in >1 location or product
  UNTRACKED_ONHAND       (medium) location balance exceeds its serial-row coverage
  COST_LAYER_MISSING_SERIAL (medium) FIFO layer for a serial product without a serial
  SERIAL_MISSING_COST_LAYER (medium) on-hand serial (FIFO) with no remaining layer
  SERIALLESS_MOVEMENT    (low)    historical qty movement of a serial product, no serial
"""
from decimal import Decimal
from django.db.models import Q, Count

from core.models import (
    Tenant, Product, InventoryLotBalance, InventoryBalance, InventoryMovement, InventoryCostLayer,
)

ZERO = Decimal("0.00")

SEVERITY = {
    "ONHAND_GT_1": "high", "NEGATIVE_SERIAL_BALANCE": "high",
    "BLANK_SERIAL_BALANCE": "high", "DUPLICATE_SERIAL": "high",
    "UNTRACKED_ONHAND": "medium", "COST_LAYER_MISSING_SERIAL": "medium",
    "SERIAL_MISSING_COST_LAYER": "medium", "SERIALLESS_MOVEMENT": "low",
}

SUGGESTION = {
    "ONHAND_GT_1": "Recount: a serial is 0 or 1 on hand. Adjust the extra units out under their real serials.",
    "NEGATIVE_SERIAL_BALANCE": "Investigate the over-issue and correct with a stock adjustment; negative serial stock is invalid.",
    "BLANK_SERIAL_BALANCE": "Assign the real serial(s) to this on-hand stock (recount), or write it off and re-receive with serials.",
    "UNTRACKED_ONHAND": "On-hand units here are not represented by serial rows; recount and receive under serials (or write off).",
    "DUPLICATE_SERIAL": "A serial is unique; it cannot be on hand in two places/products. Reconcile which one truly holds it.",
    "COST_LAYER_MISSING_SERIAL": "FIFO layer for a serial product has no serial; costing falls back to lot/average. Re-tag if the serial is known.",
    "SERIAL_MISSING_COST_LAYER": "On-hand serial has no FIFO cost layer; issues fall back to average cost. Cost-correct if needed.",
    "SERIALLESS_MOVEMENT": "Historical movement(s) without a serial — informational only; old movements are not rewritten.",
}

# Bound the serialless-movement scan so a pathological history can't blow memory.
_MOVEMENT_SCAN_CAP = 20000


def _issue(t, issue_type, *, product=None, location=None, lot_code=None, serial_number=None,
           on_hand=None, related=None, detail=None):
    return {
        "tenant_id": t.id, "tenant": t.name,
        "product_id": getattr(product, "id", None), "sku": getattr(product, "sku", ""),
        "location": getattr(location, "name", "") if location is not None else "",
        "location_id": getattr(location, "id", None),
        "lot_code": lot_code or "", "serial_number": serial_number or "",
        "on_hand": on_hand,
        "issue_type": issue_type, "severity": SEVERITY[issue_type],
        "suggestion": SUGGESTION[issue_type], "related": related or "",
        "detail": detail or "",
    }


def _audit_tenant(t):
    prods = {p.id: p for p in Product.objects.filter(tenant=t, track_serial=True)}
    if not prods:
        return []
    pids = set(prods)
    issues = []

    lots = list(InventoryLotBalance.objects.filter(tenant=t, product_id__in=pids)
                .select_related("location"))

    serial_locs = {}        # (product_id, serial) -> set(location_id)  for on_hand>0
    serial_products = {}    # serial -> set(product_id)                 for on_hand>0
    loc_serial_sum = {}     # (product_id, location_id) -> Σ serial-row on_hand

    for lb in lots:
        oh = lb.on_hand or ZERO
        sn = (lb.serial_number or "").strip()
        p = prods[lb.product_id]
        if oh < ZERO:
            issues.append(_issue(t, "NEGATIVE_SERIAL_BALANCE", product=p, location=lb.location,
                                  lot_code=lb.lot_code, serial_number=sn, on_hand=oh,
                                  related=f"lotbalance:{lb.id}"))
        if sn and oh > 1:
            issues.append(_issue(t, "ONHAND_GT_1", product=p, location=lb.location,
                                  lot_code=lb.lot_code, serial_number=sn, on_hand=oh,
                                  related=f"lotbalance:{lb.id}"))
        if (not sn) and oh > 0:
            issues.append(_issue(t, "BLANK_SERIAL_BALANCE", product=p, location=lb.location,
                                  lot_code=lb.lot_code, on_hand=oh, related=f"lotbalance:{lb.id}"))
        if sn and oh > 0:
            serial_locs.setdefault((lb.product_id, sn), set()).add(lb.location_id)
            serial_products.setdefault(sn, set()).add(lb.product_id)
            key = (lb.product_id, lb.location_id)
            loc_serial_sum[key] = loc_serial_sum.get(key, ZERO) + oh

    for (pid, sn), locs in serial_locs.items():
        if len(locs) > 1:
            issues.append(_issue(t, "DUPLICATE_SERIAL", product=prods[pid], serial_number=sn,
                                  related=f"locations:{len(locs)}",
                                  detail=f"on hand in {len(locs)} locations"))
    for sn, ps in serial_products.items():
        if len(ps) > 1:
            issues.append(_issue(t, "DUPLICATE_SERIAL", serial_number=sn,
                                  related="products:" + ",".join(prods[p].sku for p in ps),
                                  detail=f"same serial on {len(ps)} different products"))

    # Coverage: a serial product's location total should equal its serial rows.
    for b in (InventoryBalance.objects.filter(tenant=t, product_id__in=pids, on_hand__gt=0)
              .select_related("location")):
        covered = loc_serial_sum.get((b.product_id, b.location_id), ZERO)
        if (b.on_hand or ZERO) > covered:
            issues.append(_issue(t, "UNTRACKED_ONHAND", product=prods[b.product_id], location=b.location,
                                  on_hand=(b.on_hand - covered), related=f"balance:{b.id}",
                                  detail=f"{b.on_hand - covered} of {b.on_hand} on hand not on a serial row"))

    # Serialless historical movements, grouped by (product, location).
    mv_qs = (InventoryMovement.objects.filter(tenant=t, product_id__in=pids)
             .exclude(qty_delta=ZERO)
             .filter(Q(serial_number__isnull=True) | Q(serial_number="")))
    counts = {(r["product_id"], r["location_id"]): r["n"]
              for r in mv_qs.values("product_id", "location_id").annotate(n=Count("id"))}
    if counts:
        samples = {}
        for mid, pid, lid in (mv_qs.order_by("-id").values_list("id", "product_id", "location_id")[:_MOVEMENT_SCAN_CAP]):
            samples.setdefault((pid, lid), [])
            if len(samples[(pid, lid)]) < 5:
                samples[(pid, lid)].append(mid)
        from core.models import Location
        loc_names = dict(Location.objects.filter(tenant=t).values_list("id", "name"))
        for (pid, lid), n in counts.items():
            ids = samples.get((pid, lid), [])
            issues.append(_issue(t, "SERIALLESS_MOVEMENT", product=prods.get(pid),
                                  location=type("L", (), {"name": loc_names.get(lid, ""), "id": lid}),
                                  related=f"movements:{n} (e.g. {','.join(map(str, ids))})",
                                  detail=f"{n} qty movement(s) without a serial"))

    # FIFO cost layers for serial products missing a serial (qty still remaining).
    for L in (InventoryCostLayer.objects.filter(tenant=t, product_id__in=pids, qty_remaining__gt=0)
              .filter(Q(serial_number__isnull=True) | Q(serial_number=""))
              .select_related("product", "location")):
        issues.append(_issue(t, "COST_LAYER_MISSING_SERIAL", product=L.product, location=L.location,
                              lot_code=L.lot_code, on_hand=L.qty_remaining, related=f"costlayer:{L.id}"))

    # On-hand FIFO serials with no remaining cost layer (valuation falls back).
    fifo_pids = {pid for pid, p in prods.items() if p.cost_method == Product.CostMethod.FIFO}
    if fifo_pids:
        have = set(InventoryCostLayer.objects
                   .filter(tenant=t, product_id__in=fifo_pids, qty_remaining__gt=0)
                   .exclude(serial_number__isnull=True).exclude(serial_number="")
                   .values_list("product_id", "location_id", "serial_number"))
        for lb in lots:
            sn = (lb.serial_number or "").strip()
            if lb.product_id in fifo_pids and sn and (lb.on_hand or ZERO) > 0:
                if (lb.product_id, lb.location_id, sn) not in have:
                    issues.append(_issue(t, "SERIAL_MISSING_COST_LAYER", product=prods[lb.product_id],
                                          location=lb.location, lot_code=lb.lot_code, serial_number=sn,
                                          on_hand=lb.on_hand, related=f"lotbalance:{lb.id}"))
    return issues


def audit_serial_readiness(tenant=None):
    """Return a list of issue dicts for `tenant` (or every tenant). Read-only."""
    tenants = [tenant] if tenant is not None else list(Tenant.objects.all())
    out = []
    for t in tenants:
        out.extend(_audit_tenant(t))
    return out


def summarize(issues):
    """Counts by severity and by issue type."""
    by_sev, by_type = {}, {}
    for i in issues:
        by_sev[i["severity"]] = by_sev.get(i["severity"], 0) + 1
        by_type[i["issue_type"]] = by_type.get(i["issue_type"], 0) + 1
    return {"total": len(issues), "by_severity": by_sev, "by_type": by_type}
