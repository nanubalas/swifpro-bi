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

DEBIT_NORMAL = {GLAccount.Type.ASSET, GLAccount.Type.EXPENSE}
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
    income, expense = [], []
    income_total = expense_total = ZERO
    for acc, vals in balances.items():
        if acc.type == GLAccount.Type.INCOME and vals["balance"] != ZERO:
            income.append({"account": acc, "amount": vals["balance"]})
            income_total += vals["balance"]
        elif acc.type == GLAccount.Type.EXPENSE and vals["balance"] != ZERO:
            expense.append({"account": acc, "amount": vals["balance"]})
            expense_total += vals["balance"]
    return {
        "income": income,
        "expense": expense,
        "income_total": income_total,
        "expense_total": expense_total,
        "net_profit": income_total - expense_total,
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
    qs = CustomerInvoice.objects.filter(tenant=tenant, status="ISSUED").select_related("customer").prefetch_related("lines", "lines__tax_code", "payment_allocations")
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
