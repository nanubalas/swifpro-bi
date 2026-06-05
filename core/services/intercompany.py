"""Inter-company trading: a sale from one group company to another.

Creates and posts a customer invoice in the seller (AR + revenue) and a matching
expense/payable in the buyer (cost + AP), linked via InterCompanyTransaction so
consolidated reporting can eliminate the intra-group amounts. Net-only (no VAT)
to keep cross-company tax out of scope for now.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone


@transaction.atomic
def create_intercompany_sale(from_tenant, to_tenant, amount, description="", user=None):
    """Raise an inter-company sale of `amount` from `from_tenant` to `to_tenant`
    (must share a group). Returns the InterCompanyTransaction."""
    from core.models import (Customer, Supplier, CustomerInvoice, CustomerInvoiceLine,
                             Expense, GLAccount, InterCompanyTransaction)
    from core.numbering import next_invoice_number
    from core.services.gl import post_customer_invoice, post_expense

    amount = Decimal(amount)
    if amount <= Decimal("0.00"):
        raise ValueError("Amount must be positive.")
    if from_tenant.group_id is None or from_tenant.group_id != to_tenant.group_id:
        raise ValueError("Both companies must belong to the same group.")
    if from_tenant.id == to_tenant.id:
        raise ValueError("Seller and buyer must be different companies.")

    # Seller side: customer = the buyer company; post AR + revenue.
    customer, _ = Customer.objects.get_or_create(
        tenant=from_tenant, name=to_tenant.name,
        defaults={"customer_type": Customer.Type.COMPANY})
    inv = CustomerInvoice.objects.create(
        tenant=from_tenant, customer=customer,
        invoice_number=next_invoice_number(from_tenant),
        notes=description or f"Inter-company sale to {to_tenant.name}",
        is_intercompany=True)
    CustomerInvoiceLine.objects.create(
        invoice=inv, description=description or "Inter-company charge",
        qty=Decimal("1.00"), unit_price=amount, tax_code=None)
    post_customer_invoice(inv, user=user)  # description-only line -> no stock/COGS

    # Buyer side: supplier = the seller company; post cost + AP.
    supplier, _ = Supplier.objects.get_or_create(tenant=to_tenant, name=from_tenant.name)
    category = (GLAccount.objects.filter(tenant=to_tenant, code="6000").first()
                or GLAccount.objects.filter(tenant=to_tenant, type=GLAccount.Type.EXPENSE).order_by("code").first())
    exp = Expense.objects.create(
        tenant=to_tenant, expense_date=timezone.localdate(),
        payee=from_tenant.name, supplier=supplier, category=category,
        description=description or f"Inter-company purchase from {from_tenant.name}",
        net_amount=amount, paid=False, is_intercompany=True)
    post_expense(exp, user=user)  # DR expense / CR AP

    return InterCompanyTransaction.objects.create(
        group_id=from_tenant.group_id, from_tenant=from_tenant, to_tenant=to_tenant,
        amount=amount, description=description or None,
        customer_invoice=inv, expense=exp, created_by=user)
