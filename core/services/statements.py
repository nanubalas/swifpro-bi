"""Customer statements: a dated ledger of a customer's invoices, receipts,
credit notes and refunds with a running balance."""
from decimal import Decimal

from django.utils import timezone

ZERO = Decimal("0.00")


def customer_statement(tenant, customer, date_from, date_to):
    from core.models import CustomerInvoice, Payment, CreditNote

    items = []
    for inv in CustomerInvoice.objects.filter(
            tenant=tenant, customer=customer, status__in=CustomerInvoice.ISSUED_STATES):
        items.append({"date": inv.invoice_date, "type": "Invoice", "ref": inv.invoice_number,
                      "debit": inv.total, "credit": ZERO})
    for p in Payment.objects.filter(tenant=tenant, customer=customer, status="POSTED", direction="RECEIPT"):
        items.append({"date": p.payment_date, "type": "Receipt", "ref": p.reference or f"PAY-{p.id}",
                      "debit": ZERO, "credit": p.amount})
    for p in Payment.objects.filter(tenant=tenant, customer=customer, status="POSTED", direction="REFUND"):
        items.append({"date": p.payment_date, "type": "Refund", "ref": p.reference or f"REF-{p.id}",
                      "debit": p.amount, "credit": ZERO})
    for cn in CreditNote.objects.filter(tenant=tenant, customer=customer, kind="SALES", status="POSTED"):
        items.append({"date": cn.credit_note_date, "type": "Credit note", "ref": cn.credit_note_number,
                      "debit": ZERO, "credit": cn.total})

    items.sort(key=lambda x: (x["date"], x["type"]))

    opening = sum(((it["debit"] - it["credit"]) for it in items if it["date"] < date_from), ZERO)
    rows = []
    balance = opening
    for it in items:
        if date_from <= it["date"] <= date_to:
            balance += it["debit"] - it["credit"]
            rows.append({**it, "balance": balance})

    return {
        "customer": customer,
        "date_from": date_from, "date_to": date_to,
        "opening": opening,
        "rows": rows,
        "closing": balance,
        "total_debit": sum((r["debit"] for r in rows), ZERO),
        "total_credit": sum((r["credit"] for r in rows), ZERO),
    }


def default_period(tenant, today=None):
    """Default statement window: the last 12 months to today."""
    today = today or timezone.localdate()
    try:
        start = today.replace(year=today.year - 1)
    except ValueError:  # 29 Feb
        start = today.replace(year=today.year - 1, day=28)
    return start, today
