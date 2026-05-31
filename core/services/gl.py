from decimal import Decimal
from django.utils import timezone
from django.db import transaction

from core.models import JournalEntry, JournalLine, GLAccount, CustomerInvoice, SupplierInvoice, Tenant

DEFAULT_ACCOUNT_CODES = {
    "inventory": "1000",
    "bank": "1050",
    "ar": "1100",
    "ap": "2000",
    "grni": "2100",
    "vat_output": "2200",
    "vat_input": "1300",
    "sales": "4000",
    "cogs": "5000",
}

def _acc(tenant: Tenant, key: str) -> GLAccount:
    code = DEFAULT_ACCOUNT_CODES[key]
    return GLAccount.objects.get(tenant=tenant, code=code)

@transaction.atomic
def post_customer_invoice(inv: CustomerInvoice, user=None) -> JournalEntry:
    if inv.status in ("ISSUED", "PAID"):
        # idempotent: if already issued assume JE exists (for MVP)
        je = JournalEntry.objects.filter(tenant=inv.tenant, ref_type="AR_INVOICE", ref_id=inv.invoice_number).order_by("-id").first()
        if je:
            return je

    tenant = inv.tenant
    je = JournalEntry.objects.create(
        tenant=tenant,
        entry_date=inv.invoice_date,
        ref_type="AR_INVOICE",
        ref_id=inv.invoice_number,
        memo=f"AR Invoice {inv.invoice_number}",
        posted_by=user,
        posted_at=timezone.now(),
    )

    subtotal = inv.subtotal
    tax = inv.tax_total
    total = inv.total

    # DR Accounts Receivable
    JournalLine.objects.create(entry=je, account=_acc(tenant, "ar"), description="Accounts Receivable", debit=total, credit=Decimal("0.00"))
    # CR Sales
    JournalLine.objects.create(entry=je, account=_acc(tenant, "sales"), description="Sales Revenue", debit=Decimal("0.00"), credit=subtotal)
    # CR VAT Output
    if tax and tax != Decimal("0.00"):
        JournalLine.objects.create(entry=je, account=_acc(tenant, "vat_output"), description="VAT Output", debit=Decimal("0.00"), credit=tax)

    inv.status = "ISSUED"
    inv.issued_at = timezone.now()
    inv.save()

    return je

@transaction.atomic
def post_supplier_invoice(inv: SupplierInvoice, user=None) -> JournalEntry:
    if inv.status == "POSTED":
        je = JournalEntry.objects.filter(tenant=inv.tenant, ref_type="AP_INVOICE", ref_id=inv.invoice_number).order_by("-id").first()
        if je:
            return je

    tenant = inv.tenant

    # Net + input VAT from lines
    lines = list(inv.lines.all())
    subtotal = sum((l.qty * l.unit_cost for l in lines), Decimal("0.00"))
    tax = sum((l.tax_amount for l in lines), Decimal("0.00"))
    total = subtotal + tax

    je = JournalEntry.objects.create(
        tenant=tenant,
        entry_date=inv.invoice_date,
        ref_type="AP_INVOICE",
        ref_id=inv.invoice_number,
        memo=f"AP Invoice {inv.invoice_number}",
        posted_by=user,
        posted_at=timezone.now(),
    )

    # DR GRNI (assume inventory already received)
    JournalLine.objects.create(entry=je, account=_acc(tenant, "grni"), description="GRNI", debit=subtotal, credit=Decimal("0.00"))
    # DR VAT Input (reclaimable)
    if tax and tax != Decimal("0.00"):
        JournalLine.objects.create(entry=je, account=_acc(tenant, "vat_input"), description="VAT Input", debit=tax, credit=Decimal("0.00"))
    # CR Accounts Payable (gross)
    JournalLine.objects.create(entry=je, account=_acc(tenant, "ap"), description="Accounts Payable", debit=Decimal("0.00"), credit=total)

    inv.status = "POSTED"
    inv.save()
    return je


@transaction.atomic
def post_payment(payment, user=None) -> JournalEntry:
    """Post a payment to the GL and mark fully-settled invoices as paid.

    Customer receipt: DR Bank / CR Accounts Receivable.
    Supplier payment: DR Accounts Payable / CR Bank.
    """
    from core.models import Payment  # avoid circular import at module load

    if payment.status == Payment.Status.POSTED:
        je = JournalEntry.objects.filter(tenant=payment.tenant, ref_type="PAYMENT", ref_id=str(payment.id)).order_by("-id").first()
        if je:
            return je

    tenant = payment.tenant
    amount = payment.amount

    je = JournalEntry.objects.create(
        tenant=tenant,
        entry_date=payment.payment_date,
        ref_type="PAYMENT",
        ref_id=str(payment.id),
        memo=f"{payment.get_direction_display()} {payment.reference or ''}".strip(),
        posted_by=user,
        posted_at=timezone.now(),
    )

    if payment.direction == Payment.Direction.RECEIPT:
        JournalLine.objects.create(entry=je, account=_acc(tenant, "bank"), description="Bank", debit=amount, credit=Decimal("0.00"))
        JournalLine.objects.create(entry=je, account=_acc(tenant, "ar"), description="Accounts Receivable", debit=Decimal("0.00"), credit=amount)
    else:
        JournalLine.objects.create(entry=je, account=_acc(tenant, "ap"), description="Accounts Payable", debit=amount, credit=Decimal("0.00"))
        JournalLine.objects.create(entry=je, account=_acc(tenant, "bank"), description="Bank", debit=Decimal("0.00"), credit=amount)

    # Mark fully-settled invoices as paid.
    for alloc in payment.allocations.select_related("customer_invoice", "supplier_invoice").all():
        inv = alloc.customer_invoice or alloc.supplier_invoice
        if inv is None:
            continue
        if inv.outstanding <= Decimal("0.00"):
            if alloc.customer_invoice_id:
                inv.status = CustomerInvoice.Status.PAID
            else:
                # Supplier invoices have no PAID state; leave POSTED (settled).
                pass
            inv.save(update_fields=["status"])

    payment.status = Payment.Status.POSTED
    payment.save(update_fields=["status"])
    return je
