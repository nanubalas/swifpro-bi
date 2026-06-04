"""UK VAT return (MTD 9-box) computation.

The figures are derived at transaction-line level from every VAT-bearing
document in the period:

  Output (sales) tax    sales invoices (issued/paid) less sales credit notes
  Input (purchase) tax  supplier bills (posted) + posted expenses
                        less purchase credit notes

Each line carries its own tax code, so different lines on one document can have
different VAT rates. Outside-the-scope supplies are excluded from the net
sales/purchases boxes (6 and 7); zero-rated and exempt supplies are included at
a zero rate, per HMRC guidance.

NOTE: actual submission to HMRC's MTD API is intentionally stubbed -- it
requires an HMRC developer account, OAuth2 credentials, a sandbox and
fraud-prevention headers that can't be provisioned here. `submit_vat_return`
records a local submission only and is the single seam to replace with a real
HMRC client later.
"""
from decimal import Decimal

from django.utils import timezone

from core.models import (
    CustomerInvoice, SupplierInvoice, Expense, CreditNote, VatReturn,
)

ZERO = Decimal("0.00")
PENNY = Decimal("0.01")


def _treatment(tax_code):
    """(kind_label, in_boxes) for a line's tax code; missing code -> included."""
    if tax_code is None:
        return "No VAT", True
    return tax_code.get_kind_display(), tax_code.in_vat_boxes


def vat_transactions(tenant, date_from, date_to):
    """Yield one VAT record per VAT-bearing line in the period.

    Each record: date, doc_type, direction (SALES/PURCHASE), ref, party,
    description, rate, treatment, in_boxes, net, vat (net/vat signed: credit
    notes are negative so they reduce the return).
    """
    records = []

    sales = (CustomerInvoice.objects
             .filter(tenant=tenant, status__in=["ISSUED", "PAID"],
                     invoice_date__gte=date_from, invoice_date__lte=date_to)
             .select_related("customer").prefetch_related("lines", "lines__tax_code"))
    for inv in sales:
        for ln in inv.lines.all():
            treat, in_boxes = _treatment(ln.tax_code)
            records.append({
                "date": inv.invoice_date, "doc_type": "Sales invoice", "direction": "SALES",
                "ref": inv.invoice_number, "party": inv.customer.name,
                "description": ln.description or (ln.product.name if ln.product_id else ""),
                "rate": (ln.tax_code.rate if ln.tax_code else ZERO), "treatment": treat,
                "in_boxes": in_boxes, "net": ln.line_total, "vat": ln.tax_amount,
            })

    purchases = (SupplierInvoice.objects
                 .filter(tenant=tenant, status="POSTED",
                         invoice_date__gte=date_from, invoice_date__lte=date_to)
                 .select_related("supplier").prefetch_related("lines", "lines__tax_code"))
    for inv in purchases:
        for ln in inv.lines.all():
            treat, in_boxes = _treatment(ln.tax_code)
            records.append({
                "date": inv.invoice_date, "doc_type": "Purchase bill", "direction": "PURCHASE",
                "ref": inv.invoice_number, "party": inv.supplier.name,
                "description": ln.product.name if ln.product_id else "",
                "rate": (ln.tax_code.rate if ln.tax_code else ZERO), "treatment": treat,
                "in_boxes": in_boxes, "net": ln.line_total, "vat": ln.tax_amount,
            })

    expenses = (Expense.objects
                .filter(tenant=tenant, status="POSTED",
                        expense_date__gte=date_from, expense_date__lte=date_to)
                .select_related("category", "tax_code"))
    for e in expenses:
        treat, in_boxes = _treatment(e.tax_code)
        records.append({
            "date": e.expense_date, "doc_type": "Expense", "direction": "PURCHASE",
            "ref": e.reference or f"EXP-{e.id}", "party": e.payee,
            "description": e.description or e.category.name,
            "rate": (e.tax_code.rate if e.tax_code else ZERO), "treatment": treat,
            "in_boxes": in_boxes, "net": e.net_amount, "vat": e.tax_amount,
        })

    credits = (CreditNote.objects
               .filter(tenant=tenant, status="POSTED",
                       credit_note_date__gte=date_from, credit_note_date__lte=date_to)
               .select_related("customer", "supplier").prefetch_related("lines", "lines__tax_code"))
    for cn in credits:
        is_sales = cn.kind == CreditNote.Kind.SALES
        for ln in cn.lines.all():
            treat, in_boxes = _treatment(ln.tax_code)
            records.append({
                "date": cn.credit_note_date,
                "doc_type": "Sales credit note" if is_sales else "Purchase credit note",
                "direction": "SALES" if is_sales else "PURCHASE",
                "ref": cn.credit_note_number, "party": cn.party_name,
                "description": ln.description or "",
                "rate": (ln.tax_code.rate if ln.tax_code else ZERO), "treatment": treat,
                "in_boxes": in_boxes, "net": -ln.line_total, "vat": -ln.tax_amount,
            })

    records.sort(key=lambda r: (r["date"], r["direction"], r["ref"]))
    return records


def vat_summary(tenant, date_from, date_to):
    """The plain-English VAT summary plus a per-rate breakdown, computed from
    the transaction-level records."""
    records = vat_transactions(tenant, date_from, date_to)
    vat_on_sales = sum((r["vat"] for r in records if r["direction"] == "SALES"), ZERO)
    net_sales = sum((r["net"] for r in records if r["direction"] == "SALES" and r["in_boxes"]), ZERO)
    vat_reclaimable = sum((r["vat"] for r in records if r["direction"] == "PURCHASE"), ZERO)
    net_purchases = sum((r["net"] for r in records if r["direction"] == "PURCHASE" and r["in_boxes"]), ZERO)

    # Per-rate breakdown (by direction + treatment).
    breakdown = {}
    for r in records:
        key = (r["direction"], r["treatment"])
        b = breakdown.setdefault(key, {"direction": r["direction"], "treatment": r["treatment"],
                                       "net": ZERO, "vat": ZERO})
        b["net"] += r["net"]
        b["vat"] += r["vat"]
    rows = sorted(breakdown.values(), key=lambda b: (b["direction"], b["treatment"]))

    return {
        "total_sales_ex_vat": net_sales.quantize(PENNY),
        "vat_on_sales": vat_on_sales.quantize(PENNY),
        "total_purchases_ex_vat": net_purchases.quantize(PENNY),
        "vat_reclaimable": vat_reclaimable.quantize(PENNY),
        "net_vat": (vat_on_sales - vat_reclaimable).quantize(PENNY),
        "breakdown": rows,
        "record_count": len(records),
    }


def compute_vat_return(tenant, date_from, date_to):
    """Return the nine VAT boxes for the period as a dict of Decimals."""
    s = vat_summary(tenant, date_from, date_to)
    box1 = s["vat_on_sales"]
    box2 = ZERO  # acquisitions (post-Brexit GB returns: typically 0)
    box3 = (box1 + box2).quantize(PENNY)
    box4 = s["vat_reclaimable"]
    box5 = (box3 - box4).quantize(PENNY)
    sales_records = [r for r in vat_transactions(tenant, date_from, date_to) if r["direction"] == "SALES"]
    purchase_records = [r for r in vat_transactions(tenant, date_from, date_to) if r["direction"] == "PURCHASE"]
    return {
        "box1_vat_due_sales": box1,
        "box2_vat_due_acquisitions": box2,
        "box3_total_vat_due": box3,
        "box4_vat_reclaimed": box4,
        "box5_net_vat": box5,
        "box6_total_sales_ex_vat": s["total_sales_ex_vat"],
        "box7_total_purchases_ex_vat": s["total_purchases_ex_vat"],
        "box8_eu_supplies": ZERO,
        "box9_eu_acquisitions": ZERO,
        "summary": s,
        "sales_count": len(sales_records),
        "purchases_count": len(purchase_records),
    }


def save_vat_return(tenant, date_from, date_to):
    """Compute and persist (or refresh) a DRAFT VatReturn for the period."""
    figures = compute_vat_return(tenant, date_from, date_to)
    fields = {k: v for k, v in figures.items() if k.startswith("box")}
    vr, _ = VatReturn.objects.update_or_create(
        tenant=tenant, period_from=date_from, period_to=date_to,
        defaults=fields,
    )
    return vr


def submit_vat_return(vat_return, user=None):
    """STUB: locally mark the return as submitted.

    Replace the body with a real HMRC MTD `POST .../returns` call once
    credentials are available. We never silently pretend a real filing
    happened -- the reference is clearly marked LOCAL-STUB.
    """
    if vat_return.status == VatReturn.Status.SUBMITTED:
        return vat_return
    vat_return.status = VatReturn.Status.SUBMITTED
    vat_return.submitted_at = timezone.now()
    vat_return.hmrc_reference = f"LOCAL-STUB-{vat_return.id}"
    vat_return.save(update_fields=["status", "submitted_at", "hmrc_reference"])
    return vat_return
