"""UK VAT return (MTD 9-box) computation.

Figures are derived from issued customer invoices (output VAT) and posted
supplier invoices (input VAT) whose invoice_date falls in the period.

NOTE: actual submission to HMRC's MTD API is intentionally stubbed -- it
requires an HMRC developer account, OAuth2 credentials, a sandbox, and
fraud-prevention headers that can't be provisioned here. `submit_vat_return`
records a local submission only and is the single seam to replace with a real
HMRC client later.
"""
from decimal import Decimal

from django.utils import timezone

from core.models import CustomerInvoice, SupplierInvoice, VatReturn

ZERO = Decimal("0.00")


def compute_vat_return(tenant, date_from, date_to):
    """Return the nine VAT boxes for the period as a dict of Decimals."""
    sales = (CustomerInvoice.objects
             .filter(tenant=tenant, status__in=["ISSUED", "PAID"],
                     invoice_date__gte=date_from, invoice_date__lte=date_to)
             .prefetch_related("lines", "lines__tax_code"))
    purchases = (SupplierInvoice.objects
                 .filter(tenant=tenant, status="POSTED",
                         invoice_date__gte=date_from, invoice_date__lte=date_to)
                 .prefetch_related("lines", "lines__tax_code"))

    box1 = sum((inv.tax_total for inv in sales), ZERO)       # output VAT
    box6 = sum((inv.subtotal for inv in sales), ZERO)        # net sales
    box4 = sum((inv.tax_total for inv in purchases), ZERO)   # input VAT
    box7 = sum((inv.subtotal for inv in purchases), ZERO)    # net purchases
    box2 = ZERO  # acquisitions (post-Brexit GB returns: typically 0)
    box3 = box1 + box2
    box5 = box3 - box4
    return {
        "box1_vat_due_sales": box1,
        "box2_vat_due_acquisitions": box2,
        "box3_total_vat_due": box3,
        "box4_vat_reclaimed": box4,
        "box5_net_vat": box5,
        "box6_total_sales_ex_vat": box6,
        "box7_total_purchases_ex_vat": box7,
        "box8_eu_supplies": ZERO,
        "box9_eu_acquisitions": ZERO,
        "sales_count": len(sales),
        "purchases_count": len(purchases),
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
