from decimal import Decimal
from django.utils import timezone
from django.db import transaction

from core.models import JournalEntry, JournalLine, GLAccount, CustomerInvoice, SupplierInvoice, Tenant

DEFAULT_ACCOUNT_CODES = {
    "inventory": "1000",
    "inventory_in_transit": "1010",
    "bank": "1050",
    "ar": "1100",
    "ap": "2000",
    "grni": "2100",
    "accruals": "2150",
    "vat_output": "2200",
    "vat_input": "1300",
    "sales": "4000",
    "cogs": "5000",
    "ppv": "5100",
    "inventory_adjustment": "5200",
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
        site_id=inv.site_id,
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

    # Inventory + COGS: deduct stock and expense cost of goods for stocked
    # product lines. Description-only (service) lines and tenants without a
    # stock location are skipped, so service businesses are unaffected.
    _post_invoice_cogs(inv, user=user)

    return je


def _post_invoice_cogs(inv, user=None):
    """Deduct stock and post COGS for an issued customer invoice's product lines.

    Mirrors the channel sales-order behaviour (moving-average / FIFO cost via
    apply_movement; negative stock allowed). Idempotent via the COGS journal
    entry's ref, so re-issuing never double-counts."""
    from core.models import Location, InventoryMovement
    from core.services.inventory import apply_movement

    tenant = inv.tenant
    product_lines = [l for l in inv.lines.all() if l.product_id]
    if not product_lines:
        return None
    # Fulfil from the invoice's own location when set; otherwise fall back to the
    # first stock-holding warehouse (legacy behaviour for invoices with no location).
    location = inv.location if getattr(inv, "location_id", None) else None
    if location is None:
        location = (Location.objects.filter(tenant=tenant, type=Location.Type.WAREHOUSE).order_by("id").first()
                    or Location.objects.filter(tenant=tenant).order_by("id").first())
    if location is None:
        return None  # no stock location configured -> treat as non-stock sale

    # Guard against double-posting if somehow called twice.
    if JournalEntry.objects.filter(tenant=tenant, ref_type="COGS", ref_id=inv.invoice_number).exists():
        return None

    from core.services.uom import to_base_qty
    cogs_total = Decimal("0.00")
    for line in product_lines:
        # Relieve stock in the product's base unit (line qty may be in a sell UOM).
        base_qty = to_base_qty(line.product, line.qty or Decimal("0.00"), getattr(line, "uom", None))
        movement = apply_movement(
            tenant=tenant, product=line.product, location=location,
            movement_type=InventoryMovement.MovementType.SALE,
            qty_delta=base_qty * Decimal("-1"),
            ref_type="AR_INVOICE", ref_id=inv.invoice_number,
            notes=f"Invoice {inv.invoice_number}", user=user,
        )
        cogs_total += -(movement.value or Decimal("0.00"))

    if cogs_total > Decimal("0.00"):
        post_cogs(tenant, cogs_total, inv.invoice_number, user=user, entry_date=inv.invoice_date,
                  site_id=inv.site_id)
    return cogs_total

@transaction.atomic
def reverse_invoice_cogs(inv, user=None):
    """Restore stock and reverse the COGS journal for a cancelled AR invoice.

    Cancelling an issued invoice backs out revenue/AR, but the goods it shipped
    must also come back: otherwise COGS stays expensed and stock stays depleted,
    so the ledger no longer matches physical stock (C7). Idempotent - keyed on
    the invoice number it does nothing if already reversed."""
    from core.models import InventoryMovement
    from core.services.inventory import apply_movement

    tenant = inv.tenant

    # Reverse the COGS entry (DR Inventory / CR COGS) once.
    cogs_je = (JournalEntry.objects
               .filter(tenant=tenant, ref_type="COGS", ref_id=inv.invoice_number)
               .order_by("-id").first())
    already_reversed = JournalEntry.objects.filter(
        tenant=tenant, ref_type="COGS_CANCEL", ref_id=inv.invoice_number).exists()
    if cogs_je and not already_reversed:
        rev = JournalEntry.objects.create(
            tenant=tenant, site_id=cogs_je.site_id, entry_date=timezone.localdate(),
            ref_type="COGS_CANCEL", ref_id=inv.invoice_number,
            memo=f"Reverse COGS {inv.invoice_number}", posted_by=user, posted_at=timezone.now())
        for l in cogs_je.lines.all():
            JournalLine.objects.create(entry=rev, account=l.account,
                                       description="COGS reversal", debit=l.credit, credit=l.debit)

    # Restore stock for each original SALE movement, at the exact cost relieved,
    # and to the same location. Guard on the reversing ref so a re-cancel is a
    # no-op even if the COGS JE was missing.
    stock_already_restored = InventoryMovement.objects.filter(
        tenant=tenant, ref_type="AR_INVOICE_CANCEL", ref_id=inv.invoice_number).exists()
    if not stock_already_restored:
        sale_moves = InventoryMovement.objects.filter(
            tenant=tenant, ref_type="AR_INVOICE", ref_id=inv.invoice_number,
            movement_type=InventoryMovement.MovementType.SALE)
        for m in sale_moves:
            apply_movement(
                tenant=tenant, product=m.product, location=m.location,
                movement_type=InventoryMovement.MovementType.RETURN,
                qty_delta=(m.qty_delta or Decimal("0.00")) * Decimal("-1"),
                ref_type="AR_INVOICE_CANCEL", ref_id=inv.invoice_number,
                notes=f"Cancel invoice {inv.invoice_number}", user=user,
                unit_cost=m.unit_cost,
                lot_code=m.lot_code, serial_number=m.serial_number, expiry_date=m.expiry_date,
            )


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

    ap_site_id = getattr(inv.po, "site_id", None)
    if ap_site_id is None:
        receipt = getattr(inv, "receipt", None)
        ap_site_id = getattr(getattr(receipt, "received_to", None), "site_id", None)
    je = JournalEntry.objects.create(
        tenant=tenant,
        site_id=ap_site_id,
        entry_date=inv.invoice_date,
        ref_type="AP_INVOICE",
        ref_id=inv.invoice_number,
        memo=f"AP Invoice {inv.invoice_number}",
        posted_by=user,
        posted_at=timezone.now(),
    )

    # Clear GRNI at the value the goods were *received* at, and book any
    # difference vs. the billed value to Purchase Price Variance (H8). Receiving
    # credited GRNI at the receipt's goods value; billing the supplier at a
    # different price would otherwise leave a permanent unreconciled GRNI
    # balance. When no receipt is linked, fall back to clearing at the billed
    # subtotal (legacy behaviour).
    receipt = getattr(inv, "receipt", None)
    received_value = None
    if receipt is not None:
        received_value = sum((l.qty_received * l.unit_cost for l in receipt.lines.all()), Decimal("0.00"))
    grni_value = received_value if received_value is not None else subtotal
    price_variance = subtotal - grni_value  # >0 billed above receipt (unfavourable)

    # DR GRNI (clear the goods-received accrual)
    JournalLine.objects.create(entry=je, account=_acc(tenant, "grni"), description="GRNI", debit=grni_value, credit=Decimal("0.00"))
    # DR/CR Purchase Price Variance for any billed-vs-received difference.
    if price_variance > Decimal("0.00"):
        JournalLine.objects.create(entry=je, account=_acc(tenant, "ppv"), description="Purchase price variance", debit=price_variance, credit=Decimal("0.00"))
    elif price_variance < Decimal("0.00"):
        JournalLine.objects.create(entry=je, account=_acc(tenant, "ppv"), description="Purchase price variance", debit=Decimal("0.00"), credit=-price_variance)
    # DR VAT Input (reclaimable)
    if tax and tax != Decimal("0.00"):
        JournalLine.objects.create(entry=je, account=_acc(tenant, "vat_input"), description="VAT Input", debit=tax, credit=Decimal("0.00"))
    # CR Accounts Payable (gross)
    JournalLine.objects.create(entry=je, account=_acc(tenant, "ap"), description="Accounts Payable", debit=Decimal("0.00"), credit=total)

    inv.status = "POSTED"
    inv.save()

    # Mark the source PO as Billed (unless it's already closed/cancelled).
    po = getattr(inv, "po", None)
    if po is not None and po.status not in ("CLOSED", "CANCELLED", "BILLED"):
        po.status = "BILLED"
        po.save(update_fields=["status"])

    # Capture the actual billed unit costs into supplier price history.
    from core.services.purchasing import record_bill_prices
    record_bill_prices(inv)
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
    elif payment.direction == Payment.Direction.REFUND:
        # Customer refund: cash out, reversing the customer's credit/overpayment.
        JournalLine.objects.create(entry=je, account=_acc(tenant, "ar"), description="Accounts Receivable", debit=amount, credit=Decimal("0.00"))
        JournalLine.objects.create(entry=je, account=_acc(tenant, "bank"), description="Bank", debit=Decimal("0.00"), credit=amount)
    else:
        JournalLine.objects.create(entry=je, account=_acc(tenant, "ap"), description="Accounts Payable", debit=amount, credit=Decimal("0.00"))
        JournalLine.objects.create(entry=je, account=_acc(tenant, "bank"), description="Bank", debit=Decimal("0.00"), credit=amount)

    if payment.direction == Payment.Direction.REFUND:
        payment.status = Payment.Status.POSTED
        payment.save(update_fields=["status"])
        return je

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


@transaction.atomic
def post_inventory_receipt(tenant, value, ref_id, user=None, entry_date=None,
                           landed_value=Decimal("0.00"), inventory_value=None, site_id=None):
    """Capitalize received stock.

    CR GRNI (goods, = supplier liability) + CR Accruals (landed). DR Inventory
    at `inventory_value` (defaults to goods + landed for actual-cost methods).
    Under standard costing the inventory value differs from goods + landed, so
    the difference is booked to Purchase Price Variance to keep the entry
    balanced (DR PPV if unfavourable, CR PPV if favourable).
    """
    value = Decimal(value)
    landed_value = Decimal(landed_value or "0.00")
    goods_and_landed = value + landed_value
    inv = Decimal(inventory_value) if inventory_value is not None else goods_and_landed
    if goods_and_landed <= Decimal("0.00") and inv <= Decimal("0.00"):
        return None

    je = JournalEntry.objects.create(
        tenant=tenant, site_id=site_id, entry_date=entry_date or timezone.now().date(),
        ref_type="GRN", ref_id=str(ref_id), memo=f"Goods received {ref_id}",
        posted_by=user, posted_at=timezone.now(),
    )
    if inv > Decimal("0.00"):
        JournalLine.objects.create(entry=je, account=_acc(tenant, "inventory"), description="Inventory", debit=inv, credit=Decimal("0.00"))
    if value > Decimal("0.00"):
        JournalLine.objects.create(entry=je, account=_acc(tenant, "grni"), description="GRNI", debit=Decimal("0.00"), credit=value)
    if landed_value > Decimal("0.00"):
        JournalLine.objects.create(entry=je, account=_acc(tenant, "accruals"), description="Landed cost accrual", debit=Decimal("0.00"), credit=landed_value)

    # Purchase price variance balances inventory (at standard) vs actual cost.
    variance = goods_and_landed - inv  # >0 unfavourable (actual > standard)
    if variance > Decimal("0.00"):
        JournalLine.objects.create(entry=je, account=_acc(tenant, "ppv"), description="Purchase price variance", debit=variance, credit=Decimal("0.00"))
    elif variance < Decimal("0.00"):
        JournalLine.objects.create(entry=je, account=_acc(tenant, "ppv"), description="Purchase price variance", debit=Decimal("0.00"), credit=-variance)
    return je


@transaction.atomic
def post_expense(expense, user=None) -> JournalEntry:
    """Post a recorded expense to the GL.

    DR the chosen expense account (net) + DR VAT input (reclaimable tax);
    CR Bank if paid now, else CR Accounts Payable (still owed).
    """
    from core.models import Expense

    if expense.status == Expense.Status.POSTED:
        je = JournalEntry.objects.filter(tenant=expense.tenant, ref_type="EXPENSE", ref_id=str(expense.id)).order_by("-id").first()
        if je:
            return je

    tenant = expense.tenant
    net = Decimal(expense.net_amount or "0.00")
    tax = Decimal(expense.tax_amount or "0.00")
    total = net + tax

    je = JournalEntry.objects.create(
        tenant=tenant, site_id=expense.site_id, entry_date=expense.expense_date,
        ref_type="EXPENSE", ref_id=str(expense.id),
        memo=f"Expense {expense.payee} {expense.reference or ''}".strip(),
        posted_by=user, posted_at=timezone.now(),
    )
    JournalLine.objects.create(entry=je, account=expense.category, description=(expense.description or expense.payee), debit=net, credit=Decimal("0.00"))
    if tax and tax != Decimal("0.00"):
        JournalLine.objects.create(entry=je, account=_acc(tenant, "vat_input"), description="VAT Input", debit=tax, credit=Decimal("0.00"))
    credit_acc = _acc(tenant, "bank") if expense.paid else _acc(tenant, "ap")
    JournalLine.objects.create(entry=je, account=credit_acc, description=("Bank" if expense.paid else "Accounts Payable"), debit=Decimal("0.00"), credit=total)

    expense.status = Expense.Status.POSTED
    expense.posted_by = user
    expense.posted_at = timezone.now()
    expense.save(update_fields=["status", "posted_by", "posted_at"])
    return je


@transaction.atomic
def post_credit_note(cn, user=None) -> JournalEntry:
    """Post a credit note (the reverse of an invoice).

    Sales credit:    DR Sales (per line) + DR VAT Output / CR Accounts Receivable.
    Purchase credit: DR Accounts Payable / CR account (per line) + CR VAT Input.
    When linked to an invoice, that invoice's outstanding falls automatically
    (see CustomerInvoice/SupplierInvoice.credit_applied); a fully-credited
    customer invoice is marked paid.
    """
    from core.models import CreditNote, CustomerInvoice

    if cn.status == CreditNote.Status.POSTED:
        je = JournalEntry.objects.filter(tenant=cn.tenant, ref_type="CREDIT_NOTE", ref_id=str(cn.id)).order_by("-id").first()
        if je:
            return je

    tenant = cn.tenant
    lines = list(cn.lines.all())
    tax = sum((l.tax_amount for l in lines), Decimal("0.00"))
    total = cn.total

    cn_site_id = getattr(getattr(cn, "customer_invoice", None), "site_id", None)
    je = JournalEntry.objects.create(
        tenant=tenant, site_id=cn_site_id, entry_date=cn.credit_note_date,
        ref_type="CREDIT_NOTE", ref_id=str(cn.id),
        memo=f"Credit note {cn.credit_note_number}",
        posted_by=user, posted_at=timezone.now(),
    )

    # Net amounts grouped by GL account.
    by_account = {}
    default_acc = _acc(tenant, "sales") if cn.kind == CreditNote.Kind.SALES else _acc(tenant, "inventory")
    for l in lines:
        acc = l.account or default_acc
        by_account[acc] = by_account.get(acc, Decimal("0.00")) + l.line_total

    if cn.kind == CreditNote.Kind.SALES:
        for acc, amount in by_account.items():
            JournalLine.objects.create(entry=je, account=acc, description="Sales credit", debit=amount, credit=Decimal("0.00"))
        if tax:
            JournalLine.objects.create(entry=je, account=_acc(tenant, "vat_output"), description="VAT Output reversal", debit=tax, credit=Decimal("0.00"))
        JournalLine.objects.create(entry=je, account=_acc(tenant, "ar"), description="Accounts Receivable", debit=Decimal("0.00"), credit=total)
    else:
        JournalLine.objects.create(entry=je, account=_acc(tenant, "ap"), description="Accounts Payable", debit=total, credit=Decimal("0.00"))
        for acc, amount in by_account.items():
            JournalLine.objects.create(entry=je, account=acc, description="Purchase credit", debit=Decimal("0.00"), credit=amount)
        if tax:
            JournalLine.objects.create(entry=je, account=_acc(tenant, "vat_input"), description="VAT Input reversal", debit=Decimal("0.00"), credit=tax)

    cn.status = CreditNote.Status.POSTED
    cn.posted_by = user
    cn.posted_at = timezone.now()
    cn.save(update_fields=["status", "posted_by", "posted_at"])

    # Mark a fully-credited customer invoice as paid (settled).
    if cn.customer_invoice_id:
        inv = cn.customer_invoice
        if inv.outstanding <= Decimal("0.00"):
            inv.status = CustomerInvoice.Status.PAID
            inv.save(update_fields=["status"])
    return je


@transaction.atomic
def post_stock_adjustment(adj, value, user=None, entry_date=None):
    """Book the GL impact of a stock adjustment (damage / write-off / found stock).

    `value` is the signed change in inventory value (same sign as qty_delta):
      loss (value < 0): DR Inventory Adjustments expense / CR Inventory.
      gain (value > 0): DR Inventory / CR Inventory Adjustments expense (reduces loss).
    A zero-value adjustment (e.g. cost unknown) posts nothing. Idempotent via the
    STOCK_ADJ ref so re-posting never double-counts."""
    value = Decimal(value)
    if value == Decimal("0.00"):
        return None
    tenant = adj.tenant
    ref_id = str(adj.id)
    existing = JournalEntry.objects.filter(tenant=tenant, ref_type="STOCK_ADJ", ref_id=ref_id).order_by("-id").first()
    if existing:
        return existing

    je = JournalEntry.objects.create(
        tenant=tenant, site_id=getattr(adj.location, "site_id", None),
        entry_date=entry_date or timezone.now().date(),
        ref_type="STOCK_ADJ", ref_id=ref_id,
        memo=f"Stock adjustment {adj.product.sku} ({adj.get_reason_display()})",
        posted_by=user, posted_at=timezone.now(),
    )
    inv_acc = _acc(tenant, "inventory")
    adj_acc = _acc(tenant, "inventory_adjustment")
    amount = abs(value)
    if value < Decimal("0.00"):
        # Inventory decreases; recognise the loss as an expense.
        JournalLine.objects.create(entry=je, account=adj_acc, description="Inventory loss / write-off", debit=amount, credit=Decimal("0.00"))
        JournalLine.objects.create(entry=je, account=inv_acc, description="Inventory", debit=Decimal("0.00"), credit=amount)
    else:
        # Inventory increases (e.g. found stock); reduces the expense.
        JournalLine.objects.create(entry=je, account=inv_acc, description="Inventory", debit=amount, credit=Decimal("0.00"))
        JournalLine.objects.create(entry=je, account=adj_acc, description="Inventory gain", debit=Decimal("0.00"), credit=amount)
    return je


@transaction.atomic
def post_transfer_dispatch(tenant, value, ref_id, user=None, entry_date=None, site_id=None):
    """Move value from Inventory into Inventory In Transit on dispatch:
    DR Inventory In Transit / CR Inventory. Keeps the GL control account in step
    with the on-hand subledger while goods are in transit (their value sits in
    the in-transit asset account, still owned by the source site)."""
    value = Decimal(value)
    if value <= Decimal("0.00"):
        return None
    je = JournalEntry.objects.create(
        tenant=tenant, site_id=site_id, entry_date=entry_date or timezone.now().date(),
        ref_type="TRANSFER_DISPATCH", ref_id=str(ref_id), memo=f"Transfer dispatch {ref_id}",
        posted_by=user, posted_at=timezone.now())
    JournalLine.objects.create(entry=je, account=_acc(tenant, "inventory_in_transit"), description="Inventory in transit", debit=value, credit=Decimal("0.00"))
    JournalLine.objects.create(entry=je, account=_acc(tenant, "inventory"), description="Inventory", debit=Decimal("0.00"), credit=value)
    return je


@transaction.atomic
def post_transfer_receipt(tenant, value, ref_id, user=None, entry_date=None, site_id=None):
    """Move value from Inventory In Transit back into Inventory on receipt:
    DR Inventory / CR Inventory In Transit. Also used (with the source site) when
    a dispatched transfer is cancelled and stock returns to source."""
    value = Decimal(value)
    if value <= Decimal("0.00"):
        return None
    je = JournalEntry.objects.create(
        tenant=tenant, site_id=site_id, entry_date=entry_date or timezone.now().date(),
        ref_type="TRANSFER_RECEIPT", ref_id=str(ref_id), memo=f"Transfer receipt {ref_id}",
        posted_by=user, posted_at=timezone.now())
    JournalLine.objects.create(entry=je, account=_acc(tenant, "inventory"), description="Inventory", debit=value, credit=Decimal("0.00"))
    JournalLine.objects.create(entry=je, account=_acc(tenant, "inventory_in_transit"), description="Inventory in transit", debit=Decimal("0.00"), credit=value)
    return je


@transaction.atomic
def post_transfer_shortage(tenant, value, ref_id, user=None, entry_date=None, site_id=None):
    """Write off in-transit stock lost in transit: DR Inventory Adjustments /
    CR Inventory In Transit (value is a positive loss amount)."""
    value = Decimal(value)
    if value <= Decimal("0.00"):
        return None
    je = JournalEntry.objects.create(
        tenant=tenant, site_id=site_id, entry_date=entry_date or timezone.now().date(),
        ref_type="TRANSFER_SHORTAGE", ref_id=str(ref_id), memo=f"Transfer in-transit shortage {ref_id}",
        posted_by=user, posted_at=timezone.now())
    JournalLine.objects.create(entry=je, account=_acc(tenant, "inventory_adjustment"), description="In-transit shortage", debit=value, credit=Decimal("0.00"))
    JournalLine.objects.create(entry=je, account=_acc(tenant, "inventory_in_transit"), description="Inventory in transit", debit=Decimal("0.00"), credit=value)
    return je


@transaction.atomic
def post_stock_adjustment_value(tenant, value, *, ref_type, ref_id, location=None, memo=None,
                                user=None, entry_date=None):
    """Generic inventory value adjustment: DR/CR Inventory vs Inventory
    Adjustments for a signed `value` (negative = loss). Used for inventory
    impacts not tied to a StockAdjustment row (e.g. transfer in-transit
    shortage). Idempotent on (tenant, ref_type, ref_id)."""
    value = Decimal(value)
    if value == Decimal("0.00"):
        return None
    existing = JournalEntry.objects.filter(tenant=tenant, ref_type=ref_type, ref_id=str(ref_id)).order_by("-id").first()
    if existing:
        return existing
    je = JournalEntry.objects.create(
        tenant=tenant, site_id=getattr(location, "site_id", None),
        entry_date=entry_date or timezone.now().date(),
        ref_type=ref_type, ref_id=str(ref_id), memo=memo or f"Inventory adjustment {ref_id}",
        posted_by=user, posted_at=timezone.now(),
    )
    inv_acc = _acc(tenant, "inventory")
    adj_acc = _acc(tenant, "inventory_adjustment")
    amount = abs(value)
    if value < Decimal("0.00"):
        JournalLine.objects.create(entry=je, account=adj_acc, description="Inventory loss", debit=amount, credit=Decimal("0.00"))
        JournalLine.objects.create(entry=je, account=inv_acc, description="Inventory", debit=Decimal("0.00"), credit=amount)
    else:
        JournalLine.objects.create(entry=je, account=inv_acc, description="Inventory", debit=amount, credit=Decimal("0.00"))
        JournalLine.objects.create(entry=je, account=adj_acc, description="Inventory gain", debit=Decimal("0.00"), credit=amount)
    return je


@transaction.atomic
def post_cycle_count_adjustment(tenant, cc, value, user=None, entry_date=None):
    """Book the GL impact of a cycle-count variance: DR/CR Inventory vs Inventory
    Adjustments, valued identically to the inventory movements the count posted
    (lot cost for lot/serial items, product cost otherwise). Cycle counts
    previously moved stock without any GL entry, drifting the control account.
    Idempotent on the CYCLE_COUNT ref."""
    value = Decimal(value)
    if value == Decimal("0.00"):
        return None
    ref_id = str(cc.id)
    existing = JournalEntry.objects.filter(tenant=tenant, ref_type="CYCLE_COUNT", ref_id=ref_id).order_by("-id").first()
    if existing:
        return existing

    je = JournalEntry.objects.create(
        tenant=tenant, site_id=getattr(cc.location, "site_id", None),
        entry_date=entry_date or timezone.now().date(),
        ref_type="CYCLE_COUNT", ref_id=ref_id,
        memo=f"Cycle count {cc.id} variance ({cc.location.name})",
        posted_by=user, posted_at=timezone.now(),
    )
    inv_acc = _acc(tenant, "inventory")
    adj_acc = _acc(tenant, "inventory_adjustment")
    amount = abs(value)
    if value < Decimal("0.00"):
        # Net shortage: inventory decreases, recognise the loss.
        JournalLine.objects.create(entry=je, account=adj_acc, description="Cycle count shrinkage", debit=amount, credit=Decimal("0.00"))
        JournalLine.objects.create(entry=je, account=inv_acc, description="Inventory", debit=Decimal("0.00"), credit=amount)
    else:
        # Net overage: inventory increases, reduce the expense.
        JournalLine.objects.create(entry=je, account=inv_acc, description="Inventory", debit=amount, credit=Decimal("0.00"))
        JournalLine.objects.create(entry=je, account=adj_acc, description="Cycle count gain", debit=Decimal("0.00"), credit=amount)
    return je


@transaction.atomic
def reverse_payment(payment, user=None):
    """Post a reversing journal entry for a payment (used when a payment is
    deleted). Mirrors the original PAYMENT entry with debit/credit swapped, so
    the bank/AR/AP effect is backed out and the ledger stays balanced."""
    orig = (JournalEntry.objects
            .filter(tenant=payment.tenant, ref_type="PAYMENT", ref_id=str(payment.id))
            .order_by("-id").first())
    if orig is None:
        return None
    je = JournalEntry.objects.create(
        tenant=payment.tenant, entry_date=timezone.now().date(),
        ref_type="PAYMENT_REVERSAL", ref_id=str(payment.id),
        memo=f"Reversal of payment {payment.id}", posted_by=user, posted_at=timezone.now(),
    )
    for l in orig.lines.all():
        JournalLine.objects.create(entry=je, account=l.account,
                                   description=f"Reversal: {l.description or ''}".strip(),
                                   debit=l.credit, credit=l.debit)
    return je


@transaction.atomic
def post_cogs(tenant, value, ref_id, user=None, entry_date=None, site_id=None):
    """Expense cost of goods sold: DR COGS / CR Inventory.

    Idempotent on (tenant, ref_id): if a COGS entry already exists for this ref
    it is returned unchanged, so a retried/duplicated post never books a second
    COGS journal (H1). Mirrors the guards on the sibling posters."""
    value = Decimal(value)
    if value <= Decimal("0.00"):
        return None
    existing = (JournalEntry.objects
                .filter(tenant=tenant, ref_type="COGS", ref_id=str(ref_id))
                .order_by("-id").first())
    if existing:
        return existing
    je = JournalEntry.objects.create(
        tenant=tenant, site_id=site_id, entry_date=entry_date or timezone.now().date(),
        ref_type="COGS", ref_id=str(ref_id), memo=f"COGS {ref_id}",
        posted_by=user, posted_at=timezone.now(),
    )
    JournalLine.objects.create(entry=je, account=_acc(tenant, "cogs"), description="Cost of Goods Sold", debit=value, credit=Decimal("0.00"))
    JournalLine.objects.create(entry=je, account=_acc(tenant, "inventory"), description="Inventory", debit=Decimal("0.00"), credit=value)
    return je
