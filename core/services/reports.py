"""Financial reports computed from the General Ledger.

Sign conventions (balances are stored as positive debit/credit amounts on
JournalLine):
  - ASSET, EXPENSE accounts are debit-normal:  balance = debit - credit
  - LIABILITY, EQUITY, INCOME accounts are credit-normal: balance = credit - debit
"""
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from django.db.models import F, Sum

from core.models import (
    GLAccount, JournalLine, CustomerInvoice, SupplierInvoice, InventoryBalance,
    InventoryCostLayer, InventoryMovement, Product,
)

DEBIT_NORMAL = {GLAccount.Type.ASSET, GLAccount.Type.EXPENSE, GLAccount.Type.COGS}
ZERO = Decimal("0.00")


def current_financial_year(tenant, today=None):
    """Return (start_date, end_date) of the tenant's current financial year,
    based on its configured financial_year_start_month (default April)."""
    import datetime
    today = today or timezone.localdate()
    start_month = getattr(tenant, "financial_year_start_month", 4) or 4
    year = today.year if today.month >= start_month else today.year - 1
    start = datetime.date(year, start_month, 1)
    end = datetime.date(year + 1, start_month, 1) - datetime.timedelta(days=1)
    return start, end


def _q(amount):
    return (amount or ZERO)


def account_balances(tenant, date_from=None, date_to=None, site_ids=None):
    """Return {account: signed_balance} for every account, within an optional
    date window (inclusive). Signed per the account's natural side.

    When `site_ids` is given, only journal entries posted to those sites are
    included (site-dimensioned P&L / balance sheet). None = company-wide."""
    lines = JournalLine.objects.filter(entry__tenant=tenant).select_related("account", "entry")
    if date_from:
        lines = lines.filter(entry__entry_date__gte=date_from)
    if date_to:
        lines = lines.filter(entry__entry_date__lte=date_to)
    if site_ids is not None:
        lines = lines.filter(entry__site_id__in=site_ids)

    agg = (
        lines.values("account")
        .annotate(debit=Sum("debit"), credit=Sum("credit"))
    )
    by_id = {row["account"]: row for row in agg}

    result = {}
    for acc in GLAccount.objects.filter(tenant=tenant).order_by("code"):
        row = by_id.get(acc.id, {})
        debit = _q(row.get("debit"))
        credit = _q(row.get("credit"))
        if acc.type in DEBIT_NORMAL:
            balance = debit - credit
        else:
            balance = credit - debit
        result[acc] = {"debit": debit, "credit": credit, "balance": balance}
    return result


def trial_balance(tenant, date_to=None, site_ids=None):
    balances = account_balances(tenant, date_to=date_to, site_ids=site_ids)
    rows = []
    total_debit = total_credit = ZERO
    for acc, vals in balances.items():
        bal = vals["balance"]
        if vals["debit"] == ZERO and vals["credit"] == ZERO:
            continue
        # Present each account on its natural side.
        if acc.type in DEBIT_NORMAL:
            debit, credit = (bal, ZERO) if bal >= 0 else (ZERO, -bal)
        else:
            debit, credit = (ZERO, bal) if bal >= 0 else (-bal, ZERO)
        total_debit += debit
        total_credit += credit
        rows.append({"account": acc, "debit": debit, "credit": credit})
    return {
        "rows": rows,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "balanced": total_debit == total_credit,
    }


def profit_and_loss(tenant, date_from=None, date_to=None, site_ids=None):
    balances = account_balances(tenant, date_from=date_from, date_to=date_to, site_ids=site_ids)
    income, cogs, expense = [], [], []
    income_total = cogs_total = expense_total = ZERO
    for acc, vals in balances.items():
        if vals["balance"] == ZERO:
            continue
        if acc.type == GLAccount.Type.INCOME:
            income.append({"account": acc, "amount": vals["balance"]})
            income_total += vals["balance"]
        elif acc.type == GLAccount.Type.COGS:
            cogs.append({"account": acc, "amount": vals["balance"]})
            cogs_total += vals["balance"]
        elif acc.type == GLAccount.Type.EXPENSE:
            expense.append({"account": acc, "amount": vals["balance"]})
            expense_total += vals["balance"]
    gross_profit = income_total - cogs_total
    return {
        "income": income,
        "cogs": cogs,
        "expense": expense,
        "income_total": income_total,
        "cogs_total": cogs_total,
        "gross_profit": gross_profit,
        "expense_total": expense_total,
        # net_profit = gross profit - operating expenses (COGS already removed)
        "net_profit": gross_profit - expense_total,
    }


def net_income(tenant, date_to=None, site_ids=None):
    pnl = profit_and_loss(tenant, date_to=date_to, site_ids=site_ids)
    return pnl["net_profit"]


def balance_sheet(tenant, as_of=None, site_ids=None):
    balances = account_balances(tenant, date_to=as_of, site_ids=site_ids)
    assets, liabilities, equity = [], [], []
    asset_total = liability_total = equity_total = ZERO
    for acc, vals in balances.items():
        bal = vals["balance"]
        if bal == ZERO:
            continue
        if acc.type == GLAccount.Type.ASSET:
            assets.append({"account": acc, "amount": bal}); asset_total += bal
        elif acc.type == GLAccount.Type.LIABILITY:
            liabilities.append({"account": acc, "amount": bal}); liability_total += bal
        elif acc.type == GLAccount.Type.EQUITY:
            equity.append({"account": acc, "amount": bal}); equity_total += bal

    # Current-period earnings roll into equity (retained earnings).
    retained = net_income(tenant, date_to=as_of, site_ids=site_ids)
    equity_total_with_earnings = equity_total + retained

    return {
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "asset_total": asset_total,
        "liability_total": liability_total,
        "equity_total": equity_total,
        "retained_earnings": retained,
        "equity_total_with_earnings": equity_total_with_earnings,
        "liabilities_equity_total": liability_total + equity_total_with_earnings,
        "balanced": asset_total == (liability_total + equity_total_with_earnings),
    }


def _bucket(days):
    if days <= 0:
        return "current"
    if days <= 30:
        return "d1_30"
    if days <= 60:
        return "d31_60"
    if days <= 90:
        return "d61_90"
    return "d90_plus"


def _aged(items, as_of):
    buckets = {"current": ZERO, "d1_30": ZERO, "d31_60": ZERO, "d61_90": ZERO, "d90_plus": ZERO}
    rows = []
    total = ZERO
    for it in items:
        due = it["due"]
        days = (as_of - due).days if due else 0
        b = _bucket(days)
        buckets[b] += it["amount"]
        total += it["amount"]
        rows.append({**it, "days": days, "bucket": b})
    return {"rows": rows, "buckets": buckets, "total": total}


def aged_receivables(tenant, as_of=None, site_ids=None):
    as_of = as_of or timezone.localdate()
    items = []
    qs = CustomerInvoice.objects.filter(tenant=tenant, status__in=("ISSUED", "SENT")).select_related("customer").prefetch_related("lines", "lines__tax_code", "payment_allocations", "credit_notes")
    if site_ids is not None:
        qs = qs.filter(site_id__in=site_ids)
    for inv in qs:
        outstanding = inv.outstanding
        if outstanding <= ZERO:
            continue
        items.append({
            "party": inv.customer.name,
            "ref": inv.invoice_number,
            "date": inv.invoice_date,
            "due": inv.due_date or inv.invoice_date,
            "amount": outstanding,
        })
    return _aged(items, as_of)


def stock_valuation(tenant, location_ids=None):
    """On-hand quantity x moving-average cost, per product, with a grand total.

    When `location_ids` is given, only those locations are valued (used to scope
    the report to the locations a user may access)."""
    rows = []
    total = ZERO
    balances = (InventoryBalance.objects.filter(tenant=tenant)
                .select_related("product").order_by("product__sku"))
    if location_ids is not None:
        balances = balances.filter(location_id__in=location_ids)
    by_product = {}
    for b in balances:
        p = b.product
        by_product.setdefault(p, ZERO)
        by_product[p] += (b.on_hand or ZERO)
    # FIFO products are valued from remaining layers; others at average cost.
    # Scope layers to the same locations as the on-hand balances so a
    # site-restricted view sums only its own locations' layers (C5/H14).
    layer_qs = InventoryCostLayer.objects.filter(tenant=tenant, qty_remaining__gt=0)
    if location_ids is not None:
        layer_qs = layer_qs.filter(location_id__in=location_ids)
    fifo_value = {
        row["product"]: row["v"]
        for row in (layer_qs
                    .values("product")
                    .annotate(v=Sum(F("qty_remaining") * F("unit_cost"))))
    }
    for product, qty in by_product.items():
        avg = product.average_cost or ZERO
        if product.cost_method == Product.CostMethod.FIFO:
            value = (fifo_value.get(product.id, ZERO)).quantize(Decimal("0.01"))
        else:
            value = (qty * avg).quantize(Decimal("0.01"))
        total += value
        rows.append({"product": product, "qty": qty, "avg_cost": avg, "value": value})
    rows.sort(key=lambda r: r["product"].sku)
    return {"rows": rows, "total": total}


def inventory_analytics(tenant, date_from, date_to, location_ids=None):
    """Inventory valuation depth + turnover KPIs for the period.

    - current_value: total stock value (per stock_valuation).
    - by_location: on-hand value per location.
    - lots: per lot/serial/expiry balance for lot-tracked stock (with value).
    - cogs: cost of goods sold posted in the period (GL account 5000).
    - turnover: annualised COGS / current stock value.
    - days_inventory: days to deplete current stock at the period's COGS run-rate.

    `location_ids` scopes valuation/lot detail to the given locations (used to
    honour a user's per-location access)."""
    from core.models import InventoryLotBalance
    val = stock_valuation(tenant, location_ids=location_ids)
    current_value = val["total"]

    # Per (product, location) FIFO value from remaining layers, so FIFO stock is
    # valued at its actual layer cost at each location instead of the company
    # average. Without this the per-location totals don't reconcile to the
    # grand total or to a site's inventory GL account (H14).
    fifo_loc_value = {}
    flayer_qs = InventoryCostLayer.objects.filter(tenant=tenant, qty_remaining__gt=0)
    if location_ids is not None:
        flayer_qs = flayer_qs.filter(location_id__in=location_ids)
    for row in (flayer_qs.values("product", "location")
                .annotate(v=Sum(F("qty_remaining") * F("unit_cost")))):
        fifo_loc_value[(row["product"], row["location"])] = row["v"] or ZERO

    # Value on hand per location (FIFO from layers; others at moving-average /
    # standard cost).
    loc_map = {}
    bal_qs = InventoryBalance.objects.filter(tenant=tenant).select_related("product", "location")
    if location_ids is not None:
        bal_qs = bal_qs.filter(location_id__in=location_ids)
    for b in bal_qs:
        if not b.on_hand:
            continue
        if b.product.cost_method == Product.CostMethod.FIFO:
            value = fifo_loc_value.get((b.product_id, b.location_id), ZERO)
        else:
            cost = b.product.average_cost or b.product.standard_cost or ZERO
            value = b.on_hand * cost
        e = loc_map.setdefault(b.location_id, {"location": b.location, "qty": ZERO, "value": ZERO})
        e["qty"] += b.on_hand
        e["value"] += value
    for e in loc_map.values():
        e["value"] = e["value"].quantize(Decimal("0.01"))
    by_location = sorted(loc_map.values(), key=lambda r: r["value"], reverse=True)

    # Lot / serial / expiry detail. Value each lot from its OWN remaining cost
    # layers (lot-specific cost), consistent with lot-scoped FIFO costing. Fall
    # back to the product's average/standard cost only when the lot has no cost
    # layer (e.g. non-FIFO products, or pre-costing legacy lots) (lot-valuation fix).
    from core.services.inventory import lot_layer_unit_cost
    lots = []
    lot_qs = (InventoryLotBalance.objects.filter(tenant=tenant, on_hand__gt=0)
              .select_related("product", "location").order_by("expiry_date", "product__sku"))
    if location_ids is not None:
        lot_qs = lot_qs.filter(location_id__in=location_ids)
    for lb in lot_qs:
        lot_cost = lot_layer_unit_cost(tenant, lb.product, lb.location,
                                       lot_code=lb.lot_code, serial_number=lb.serial_number,
                                       expiry_date=lb.expiry_date)
        cost = lot_cost if lot_cost is not None else (lb.product.average_cost or lb.product.standard_cost or ZERO)
        lots.append({
            "product": lb.product, "location": lb.location, "lot": lb.lot_code,
            "serial": lb.serial_number, "expiry": lb.expiry_date, "qty": lb.on_hand,
            "value": (lb.on_hand * cost).quantize(Decimal("0.01")),
        })

    # Turnover KPIs from period COGS (GL account 5000, debit-normal).
    agg = (JournalLine.objects
           .filter(entry__tenant=tenant, account__code="5000",
                   entry__entry_date__gte=date_from, entry__entry_date__lte=date_to)
           .aggregate(d=Sum("debit"), c=Sum("credit")))
    cogs = (agg["d"] or ZERO) - (agg["c"] or ZERO)
    period_days = max((date_to - date_from).days + 1, 1)
    annual_cogs = cogs * Decimal(365) / Decimal(period_days)
    turnover = (annual_cogs / current_value).quantize(Decimal("0.01")) if current_value else None
    days_inventory = ((current_value * Decimal(period_days) / cogs).quantize(Decimal("0.1"))
                      if cogs > ZERO else None)
    return {
        "current_value": current_value, "by_location": by_location, "lots": lots,
        "cogs": cogs, "turnover": turnover, "days_inventory": days_inventory,
        "period_days": period_days,
    }


def inventory_gl_reconciliation(tenant, date_from=None, date_to=None, site_id=None,
                                account_key="inventory"):
    """Reconcile the inventory subledger (InventoryMovement.value) against the GL
    inventory control account, so a missed/duplicated/failed posting is caught
    instead of drifting silently.

    Returns opening/movement-debits/movement-credits/closing for the subledger,
    the GL account's closing balance, and the variance (subledger - GL), plus
    drill-down lists of the movement IDs and journal-entry IDs in the window.

    Sign convention: InventoryMovement.value is +inbound / -outbound, matching
    the inventory account's debit-normal balance (debits - credits).

    Scope: reconcile at tenant level (optionally a period). A `site_id` filter is
    supported, but note inter-site transfers post inventory movements at both
    sites yet create no GL entry, so per-site variance can be non-zero by design;
    the tenant-level (no site filter) figure is the authoritative control check.
    """
    from core.services.gl import DEFAULT_ACCOUNT_CODES
    ZERO = Decimal("0.00")
    code = DEFAULT_ACCOUNT_CODES[account_key]
    account = GLAccount.objects.filter(tenant=tenant, code=code).first()

    # ---- Subledger (inventory movements) ----
    mv = InventoryMovement.objects.filter(tenant=tenant, value__isnull=False)
    if site_id is not None:
        mv = mv.filter(site_id=site_id)

    def _mv_sum(qs):
        return qs.aggregate(s=Sum("value"))["s"] or ZERO

    opening_sub = ZERO
    period_mv = mv
    if date_from is not None:
        opening_sub = _mv_sum(mv.filter(created_at__date__lt=date_from))
        period_mv = period_mv.filter(created_at__date__gte=date_from)
    if date_to is not None:
        period_mv = period_mv.filter(created_at__date__lte=date_to)
    movement_debits = _mv_sum(period_mv.filter(value__gt=0))
    movement_credits = -(_mv_sum(period_mv.filter(value__lt=0)))  # positive magnitude
    closing_sub = opening_sub + movement_debits - movement_credits

    # ---- GL inventory control account ----
    gl = JournalLine.objects.filter(entry__tenant=tenant, account__code=code)
    if site_id is not None:
        gl = gl.filter(entry__site_id=site_id)

    def _gl_net(qs):
        a = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
        return (a["d"] or ZERO) - (a["c"] or ZERO)

    opening_gl = ZERO
    period_gl = gl
    if date_from is not None:
        opening_gl = _gl_net(gl.filter(entry__entry_date__lt=date_from))
        period_gl = period_gl.filter(entry__entry_date__gte=date_from)
    if date_to is not None:
        period_gl = period_gl.filter(entry__entry_date__lte=date_to)
    gl_agg = period_gl.aggregate(d=Sum("debit"), c=Sum("credit"))
    gl_debits = gl_agg["d"] or ZERO
    gl_credits = gl_agg["c"] or ZERO
    closing_gl = opening_gl + gl_debits - gl_credits

    variance = (closing_sub - closing_gl).quantize(Decimal("0.01"))

    return {
        "account_code": code,
        "account": account,
        "date_from": date_from,
        "date_to": date_to,
        "site_id": site_id,
        # Subledger
        "opening_subledger": opening_sub.quantize(Decimal("0.01")),
        "movement_debits": movement_debits.quantize(Decimal("0.01")),
        "movement_credits": movement_credits.quantize(Decimal("0.01")),
        "closing_subledger": closing_sub.quantize(Decimal("0.01")),
        # GL
        "opening_gl": opening_gl.quantize(Decimal("0.01")),
        "gl_debits": gl_debits.quantize(Decimal("0.01")),
        "gl_credits": gl_credits.quantize(Decimal("0.01")),
        "closing_gl": closing_gl.quantize(Decimal("0.01")),
        # Result
        "variance": variance,
        "balanced": variance == Decimal("0.00"),
        # Drill-down
        "movement_ids": list(period_mv.order_by("id").values_list("id", flat=True)),
        "journal_entry_ids": sorted(set(period_gl.values_list("entry_id", flat=True))),
    }


def inventory_gl_reconciliation_by_location(tenant, date_from=None, date_to=None):
    """Subledger inventory value per location (movements only), as drill-down for
    the control-account reconciliation. The GL inventory account is not split by
    location, so this is the subledger side only."""
    ZERO = Decimal("0.00")
    mv = InventoryMovement.objects.filter(tenant=tenant, value__isnull=False)
    if date_from is not None:
        mv = mv.filter(created_at__date__gte=date_from)
    if date_to is not None:
        mv = mv.filter(created_at__date__lte=date_to)
    rows = (mv.values("location_id", "location__name")
            .annotate(value=Sum("value")).order_by("location__name"))
    return [{"location_id": r["location_id"], "location": r["location__name"],
             "value": (r["value"] or ZERO).quantize(Decimal("0.01"))} for r in rows]


def check_inventory_gl_variance(tenant=None, tolerance=Decimal("0.01"), as_of=None):
    """Periodic control check: flag tenants whose inventory subledger and GL
    control account differ by more than `tolerance` (as-of `as_of`, default all
    history). Returns a list of reconciliation dicts that breach tolerance."""
    from core.models import Tenant
    tenants = [tenant] if tenant is not None else list(Tenant.objects.all())
    flagged = []
    for t in tenants:
        rec = inventory_gl_reconciliation(t, date_to=as_of)
        if abs(rec["variance"]) > tolerance:
            rec["tenant"] = t
            flagged.append(rec)
    return flagged


def near_expiry_lots(tenant, days=30, location_ids=None, product_id=None, status=None,
                     include_zero=False, today=None):
    """Lots/serials at or near expiry, from existing lot-balance + expiry data.

    Returns a row per expiry-dated lot balance with days-until-expiry, on-hand /
    reserved / available, lot value (from the lot's own cost layer where one
    exists, else the product average/standard cost), and a status of
    expired / near_expiry / okay.

    Default view shows expired + near (within `days`); pass status to narrow to a
    single bucket, or status='all' to include okay lots too. Zero-balance lots
    are excluded unless include_zero=True (audit). `today` is injectable for tests.
    """
    from core.models import InventoryLotBalance
    from core.services.inventory import lot_layer_unit_cost
    today = today or timezone.localdate()
    days = int(days)

    qs = (InventoryLotBalance.objects
          .filter(tenant=tenant, expiry_date__isnull=False)
          .select_related("product", "location"))
    if not include_zero:
        qs = qs.filter(on_hand__gt=0)
    if location_ids is not None:
        qs = qs.filter(location_id__in=location_ids)
    if product_id:
        qs = qs.filter(product_id=product_id)

    rows = []
    for lb in qs.order_by("expiry_date", "product__sku"):
        days_until = (lb.expiry_date - today).days
        if days_until < 0:
            st = "expired"
        elif days_until <= days:
            st = "near_expiry"
        else:
            st = "okay"
        if status in ("expired", "near_expiry", "okay"):
            if st != status:
                continue
        elif status != "all" and st == "okay":
            # Default view: expired + near only.
            continue
        unit = lot_layer_unit_cost(tenant, lb.product, lb.location, lot_code=lb.lot_code,
                                   serial_number=lb.serial_number, expiry_date=lb.expiry_date)
        cost = unit if unit is not None else (lb.product.average_cost or lb.product.standard_cost or ZERO)
        on_hand = lb.on_hand or ZERO
        reserved = lb.reserved or ZERO
        rows.append({
            "product": lb.product, "location": lb.location,
            "lot_code": lb.lot_code, "serial_number": lb.serial_number,
            "expiry_date": lb.expiry_date, "days_until": days_until,
            "on_hand": on_hand, "reserved": reserved, "available": on_hand - reserved,
            "value": (on_hand * cost).quantize(Decimal("0.01")),
            "valuation_source": "lot_layer" if unit is not None else "product_cost",
            "status": st,
        })
    return rows


def lot_trace(tenant, product_id, lot_code, serial_number=None, location_ids=None):
    """Full movement history + costing trail + current balances for one lot
    (scoped by tenant + product + lot, and serial when given - lot codes are not
    assumed globally unique).

    Returns: product, lot/serial, movements (each annotated with its GRN / PO /
    supplier source for receipts and its InventoryIssueCost lines for issues),
    cost layers (receipt cost + remaining), and per-location lot balances.
    """
    from core.models import (InventoryMovement, InventoryIssueCost, InventoryCostLayer,
                             InventoryLotBalance, GoodsReceipt, Product)

    product = Product.objects.filter(tenant=tenant, id=product_id).first()
    if product is None:
        return None

    mv = InventoryMovement.objects.filter(tenant=tenant, product_id=product_id, lot_code=lot_code)
    if serial_number:
        mv = mv.filter(serial_number=serial_number)
    if location_ids is not None:
        mv = mv.filter(location_id__in=location_ids)
    moves = list(mv.select_related("product", "location", "bin").order_by("created_at", "id"))

    ic_map = {}
    for ic in (InventoryIssueCost.objects
               .filter(tenant=tenant, movement_id__in=[m.id for m in moves])
               .select_related("cost_layer")):
        ic_map.setdefault(ic.movement_id, []).append(ic)

    grn_numbers = [m.ref_id for m in moves if m.ref_type == "GRN" and m.ref_id]
    grns = {g.grn_number: g for g in (GoodsReceipt.objects
            .filter(tenant=tenant, grn_number__in=grn_numbers)
            .select_related("po", "po__supplier"))}

    movements = []
    for m in moves:
        grn = grns.get(m.ref_id) if m.ref_type == "GRN" else None
        po = getattr(grn, "po", None)
        movements.append({
            "m": m, "grn": grn, "po": po,
            "supplier": getattr(po, "supplier", None),
            "issue_costs": ic_map.get(m.id, []),
        })

    layer_q = InventoryCostLayer.objects.filter(tenant=tenant, product_id=product_id, lot_code=lot_code)
    if serial_number:
        layer_q = layer_q.filter(serial_number=serial_number)
    if location_ids is not None:
        layer_q = layer_q.filter(location_id__in=location_ids)
    layers = list(layer_q.select_related("location").order_by("received_at", "id"))

    bal_q = InventoryLotBalance.objects.filter(tenant=tenant, product_id=product_id, lot_code=lot_code)
    if serial_number:
        bal_q = bal_q.filter(serial_number=serial_number)
    if location_ids is not None:
        bal_q = bal_q.filter(location_id__in=location_ids)
    balances = list(bal_q.select_related("location"))

    return {
        "product": product, "lot_code": lot_code, "serial_number": serial_number,
        "movements": movements, "layers": layers, "balances": balances,
    }


def cash_flow_summary(tenant, date_from=None, date_to=None):
    """A simple cash flow summary built from movements on the Bank account.

    Opening + (cash in - cash out) = closing. Movements are grouped by the
    other side of each entry (the source/use of cash) so a business owner can
    see where money came from and went, without reading the ledger.
    """
    bank = GLAccount.objects.filter(tenant=tenant, code="1050").first()
    if bank is None:
        return {"rows": [], "cash_in": ZERO, "cash_out": ZERO, "net": ZERO,
                "opening": ZERO, "closing": ZERO, "date_from": date_from, "date_to": date_to}

    # Opening balance = bank balance strictly before the period.
    opening = ZERO
    if date_from:
        import datetime
        day_before = date_from - datetime.timedelta(days=1)
        opening = account_balances(tenant, date_to=day_before).get(bank, {}).get("balance", ZERO)

    bank_lines = (JournalLine.objects.filter(entry__tenant=tenant, account=bank)
                  .select_related("entry").prefetch_related("entry__lines", "entry__lines__account"))
    if date_from:
        bank_lines = bank_lines.filter(entry__entry_date__gte=date_from)
    if date_to:
        bank_lines = bank_lines.filter(entry__entry_date__lte=date_to)

    by_account = {}
    cash_in = cash_out = ZERO
    for bl in bank_lines:
        delta = (bl.debit or ZERO) - (bl.credit or ZERO)  # + in / - out
        if delta > ZERO:
            cash_in += delta
        else:
            cash_out += -delta
        counters = [l for l in bl.entry.lines.all() if l.account_id != bank.id]
        total_counter = sum((abs((l.debit or ZERO) - (l.credit or ZERO)) for l in counters), ZERO)
        if total_counter == ZERO:
            by_account[bank] = by_account.get(bank, ZERO) + delta
            continue
        for l in counters:
            weight = abs((l.debit or ZERO) - (l.credit or ZERO)) / total_counter
            by_account[l.account] = by_account.get(l.account, ZERO) + (delta * weight)

    rows = [{"account": acc, "amount": amt.quantize(Decimal("0.01"))}
            for acc, amt in by_account.items() if amt.quantize(Decimal("0.01")) != ZERO]
    rows.sort(key=lambda r: r["amount"], reverse=True)
    net = cash_in - cash_out
    return {
        "rows": rows, "cash_in": cash_in, "cash_out": cash_out, "net": net,
        "opening": opening, "closing": opening + net,
        "date_from": date_from, "date_to": date_to,
    }


def aged_payables(tenant, as_of=None, site_ids=None):
    as_of = as_of or timezone.localdate()
    items = []
    qs = SupplierInvoice.objects.filter(tenant=tenant, status="POSTED").select_related("supplier").prefetch_related("lines", "lines__tax_code", "payment_allocations")
    if site_ids is not None:
        qs = qs.filter(po__site_id__in=site_ids)
    for inv in qs:
        outstanding = inv.outstanding
        if outstanding <= ZERO:
            continue
        items.append({
            "party": inv.supplier.name,
            "ref": inv.invoice_number,
            "date": inv.invoice_date,
            "due": inv.invoice_date,
            "amount": outstanding,
        })
    return _aged(items, as_of)


def consolidated(companies, date_from=None, date_to=None, as_of=None):
    """Combine P&L, balance sheet and stock value across several companies.

    `companies` is an iterable of Tenant. Returns per-company rows plus a group
    total for each statement, so a group can see combined performance. (Pure
    aggregation; inter-company eliminations are applied separately.)"""
    rows = []
    tot = {"revenue": ZERO, "cogs": ZERO, "gross": ZERO, "expenses": ZERO, "net": ZERO,
           "assets": ZERO, "liabilities": ZERO, "equity": ZERO, "stock": ZERO}
    for t in companies:
        pnl = profit_and_loss(t, date_from=date_from, date_to=date_to)
        bs = balance_sheet(t, as_of=as_of)
        stock = stock_valuation(t)["total"]
        row = {
            "company": t,
            "revenue": pnl["income_total"], "cogs": pnl["cogs_total"],
            "gross": pnl["gross_profit"], "expenses": pnl["expense_total"],
            "net": pnl["net_profit"],
            "assets": bs["asset_total"],
            "liabilities": bs["liability_total"],
            "equity": bs["equity_total_with_earnings"],
            "stock": stock,
        }
        rows.append(row)
        for k in tot:
            tot[k] += row[k]

    # Inter-company eliminations: sales/purchases purely between the companies in
    # scope are intra-group and shouldn't inflate consolidated figures.
    from core.models import InterCompanyTransaction
    ids = [t.id for t in companies]
    elim = (InterCompanyTransaction.objects
            .filter(from_tenant_id__in=ids, to_tenant_id__in=ids)
            .aggregate(s=Sum("amount"))["s"] or ZERO)
    tot["eliminations"] = elim
    tot["net_revenue"] = tot["revenue"] - elim
    tot["net_expenses"] = tot["expenses"] - elim
    return {"rows": rows, "totals": tot}
