from decimal import Decimal
from django.db.models.signals import post_save
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from core.models import Tenant, TaxCode, GLAccount, Location
from core.audit import log_audit

DEFAULT_TAX_CODES = [
    ("STD", "Standard rate (20%)", Decimal("0.20"), "STANDARD"),
    ("RED", "Reduced rate (5%)", Decimal("0.05"), "REDUCED"),
    ("ZERO", "Zero rate (0%)", Decimal("0.00"), "ZERO"),
    ("EXEMPT", "Exempt", Decimal("0.00"), "EXEMPT"),
    ("OS", "Outside the scope of VAT", Decimal("0.00"), "OUTSIDE"),
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
    ("5000", "Cost of Goods Sold", "COGS"),
    ("5100", "Purchase Price Variance", "EXPENSE"),
    ("5200", "Inventory Adjustments / Shrinkage", "EXPENSE"),
    # Operating expense accounts (used by the Expenses module).
    ("6000", "General Expenses", "EXPENSE"),
    ("6100", "Rent & Rates", "EXPENSE"),
    ("6150", "Repairs & Maintenance", "EXPENSE"),
    ("6200", "Utilities", "EXPENSE"),
    ("6250", "Insurance", "EXPENSE"),
    ("6300", "Office & Admin", "EXPENSE"),
    ("6400", "Travel & Subsistence", "EXPENSE"),
    ("6450", "Meals & Entertainment", "EXPENSE"),
    ("6500", "Marketing", "EXPENSE"),
    ("6600", "Professional Fees", "EXPENSE"),
    ("6700", "Software & Subscriptions", "EXPENSE"),
    ("6900", "Other Expenses", "EXPENSE"),
]

@receiver(post_save, sender=Tenant)
def bootstrap_tenant_defaults(sender, instance: Tenant, created: bool, **kwargs):
    if not created:
        return

    # Tax codes
    for code, name, rate, kind in DEFAULT_TAX_CODES:
        TaxCode.objects.get_or_create(tenant=instance, code=code, defaults={"name": name, "rate": rate, "kind": kind})

    # GL accounts
    for code, name, acc_type in DEFAULT_ACCOUNTS:
        GLAccount.objects.get_or_create(tenant=instance, code=code, defaults={"name": name, "type": acc_type})

    # Default stock location so a fresh organisation can receive stock, raise
    # POs and fulfil sales without a manual setup step. Renameable later, and
    # more locations can be added. Only ever seeded when the org has none.
    if not Location.objects.filter(tenant=instance).exists():
        Location.objects.create(
            tenant=instance, name="Main Location",
            type=Location.Type.WAREHOUSE, holds_stock=True, is_active=True,
        )


@receiver(post_save, sender="core.OrgMembership")
def _sync_membership_groups(sender, instance, **kwargs):
    """Keep a member's Django groups in sync with their org role so the
    existing per-view RBAC enforces correctly."""
    from django.contrib.auth.models import Group
    from core.roles import ROLE_TO_GROUPS
    for name in ROLE_TO_GROUPS.get(instance.role, []):
        group, _ = Group.objects.get_or_create(name=name)
        instance.user.groups.add(group)


@receiver(user_logged_in)
def _audit_login(sender, request, user, **kwargs):
    tenant = None
    try:
        from core.access import get_active_tenant
        tenant = get_active_tenant(request)
    except Exception:
        tenant = None
    log_audit(action="LOGIN", request=request, user=user, tenant=tenant)


@receiver(user_logged_out)
def _audit_logout(sender, request, user, **kwargs):
    log_audit(action="LOGOUT", request=request, user=user)


@receiver(user_login_failed)
def _audit_login_failed(sender, credentials, request=None, **kwargs):
    log_audit(action="LOGIN_FAILED", request=request, username=(credentials or {}).get("username"))
