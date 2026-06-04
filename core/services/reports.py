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
    InventoryCostLayer, Product,
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


def account_balances(tenant, date_from=None, date_to=None):
    """Return {account: signed_balance} for every account, within an optional
    date window (inclusive). Signed per the account's natural side."""
    lines = JournalLine.objects.filter(entry__tenant=tenant).select_related("account", "entry")
    if date_from:
        lines = lines.filter(entry__entry_date__gte=date_from)
    if date_to:
        lines = lines.filter(entry__entry_date__lte=date_to)

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


def trial_balance(tenant, date_to=None):
    balances = account_balances(tenant, date_to=date_to)
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


def profit_and_loss(tenant, date_from=None, date_to=None):
    balances = account_balances(tenant, date_from=date_from, date_to=date_to)
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


def net_income(tenant, date_to=None):
    pnl = profit_and_loss(tenant, date_to=date_to)
    return pnl["net_profit"]


def balance_sheet(tenant, as_of=None):
    balances = account_balances(tenant, date_to=as_of)
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
    retained = net_income(tenant, date_to=as_of)
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


def aged_receivables(tenant, as_of=None):
    as_of = as_of or timezone.localdate()
    items = []
    qs = CustomerInvoice.objects.filter(tenant=tenant, status__in=("ISSUED", "SENT")).select_related("customer").prefetch_related("lines", "lines__tax_code", "payment_allocations", "credit_notes")
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


def stock_valuation(tenant):
    """On-hand quantity x moving-average cost, per product, with a grand total."""
    rows = []
    total = ZERO
    balances = (InventoryBalance.objects.filter(tenant=tenant)
                .select_related("product").order_by("product__sku"))
    by_product = {}
    for b in balances:
        p = b.product
        by_product.setdefault(p, ZERO)
        by_product[p] += (b.on_hand or ZERO)
    # FIFO products are valued from remaining layers; others at average cost.
    fifo_value = {
        row["product"]: row["v"]
        for row in (InventoryCostLayer.objects
                    .filter(tenant=tenant, qty_remaining__gt=0)
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


def inventory_analytics(tenant, date_from, date_to):
    """Inventory valuation depth + turnover KPIs for the period.

    - current_value: total stock value (per stock_valuation).
    - by_location: on-hand value per location.
    - lots: per lot/serial/expiry balance for lot-tracked stock (with value).
    - cogs: cost of goods sold posted in the period (GL account 5000).
    - turnover: annualised COGS / current stock value.
    - days_inventory: days to deplete current stock at the period's COGS run-rate.
    """
    from core.models import InventoryLotBalance
    val = stock_valuation(tenant)
    current_value = val["total"]

    # Value on hand per location (at moving-average / standard cost).
    loc_map = {}
    for b in (InventoryBalance.objects.filter(tenant=tenant).select_related("product", "location")):
        if not b.on_hand:
            continue
        cost = b.product.average_cost or b.product.standard_cost or ZERO
        e = loc_map.setdefault(b.location_id, {"location": b.location, "qty": ZERO, "value": ZERO})
        e["qty"] += b.on_hand
        e["value"] += (b.on_hand * cost)
    for e in loc_map.values():
        e["value"] = e["value"].quantize(Decimal("0.01"))
    by_location = sorted(loc_map.values(), key=lambda r: r["value"], reverse=True)

    # Lot / serial / expiry detail.
    lots = []
    for lb in (InventoryLotBalance.objects.filter(tenant=tenant, on_hand__gt=0)
               .select_related("product", "location").order_by("expiry_date", "product__sku")):
        cost = lb.product.average_cost or lb.product.standard_cost or ZERO
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


def aged_payables(tenant, as_of=None):
    as_of = as_of or timezone.localdate()
    items = []
    qs = SupplierInvoice.objects.filter(tenant=tenant, status="POSTED").select_related("supplier").prefetch_related("lines", "lines__tax_code", "payment_allocations")
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
