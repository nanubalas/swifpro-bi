from decimal import Decimal
from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models import Tenant, TaxCode, GLAccount

DEFAULT_TAX_CODES = [
    ("STD", "Standard rate", Decimal("0.20")),
    ("ZERO", "Zero rate", Decimal("0.00")),
    ("EXEMPT", "Exempt", Decimal("0.00")),
]

DEFAULT_ACCOUNTS = [
    ("1000", "Inventory", "ASSET"),
    ("1050", "Bank", "ASSET"),
    ("1100", "Accounts Receivable", "ASSET"),
    ("2000", "Accounts Payable", "LIABILITY"),
    ("2100", "GRNI (Goods Received Not Invoiced)", "LIABILITY"),
    ("2150", "Accruals (Landed Costs)", "LIABILITY"),
    ("2200", "VAT Output", "LIABILITY"),
    ("1300", "VAT Input", "ASSET"),
    ("4000", "Sales Revenue", "INCOME"),
    ("5000", "Cost of Goods Sold", "EXPENSE"),
]

@receiver(post_save, sender=Tenant)
def bootstrap_tenant_defaults(sender, instance: Tenant, created: bool, **kwargs):
    if not created:
        return

    # Tax codes
    for code, name, rate in DEFAULT_TAX_CODES:
        TaxCode.objects.get_or_create(tenant=instance, code=code, defaults={"name": name, "rate": rate})

    # GL accounts
    for code, name, acc_type in DEFAULT_ACCOUNTS:
        GLAccount.objects.get_or_create(tenant=instance, code=code, defaults={"name": name, "type": acc_type})
