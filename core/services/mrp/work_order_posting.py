"""Manufacturing GL / WIP posting for work orders (Phase 7).

Posts balanced journals as work orders execute, reusing the existing
JournalEntry / JournalLine / GLAccount models and the (tenant, ref_type, ref_id)
idempotency pattern (backed by the DB unique constraint on journal references):

    material issue  : DR WIP                       CR Raw Material Inventory
    completion      : DR Finished Goods Inventory  CR WIP
    close variance  : remaining WIP -> Manufacturing Variance (either direction)

Accounts come from a ManufacturingAccountingProfile (site-specific, else the
tenant default). When no active profile exists, posting is SKIPPED so inventory
still moves and the Phase 6 behaviour is preserved. When a profile exists but a
required account is unset, a MissingManufacturingAccount is raised so the caller
can warn the planner without breaking the inventory movement.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

ZERO = Decimal("0.00")

REF_ISSUE = "WORK_ORDER_ISSUE"
REF_COMPLETION = "WORK_ORDER_COMPLETION"
REF_VARIANCE = "WORK_ORDER_VARIANCE"
# Phase 12 correction journals (idempotent on the correction movement id).
REF_SCRAP = "WORK_ORDER_SCRAP"
REF_COMPLETION_SCRAP = "WORK_ORDER_COMPLETION_SCRAP"
REF_ISSUE_REVERSAL = "WORK_ORDER_ISSUE_REVERSAL"
REF_COMPLETION_REVERSAL = "WORK_ORDER_COMPLETION_REVERSAL"


class MissingManufacturingAccount(Exception):
    """A manufacturing profile exists but a required account is not configured."""
    def __init__(self, account_key, message):
        self.account_key = account_key
        super().__init__(message)


def get_profile(tenant, site):
    """Resolve the manufacturing accounting profile for a site, else the tenant
    default. Returns None when none is configured (posting then skipped)."""
    from core.models import ManufacturingAccountingProfile
    qs = ManufacturingAccountingProfile.objects.filter(tenant=tenant, is_active=True)
    return (qs.filter(site=site).first()
            or qs.filter(site__isnull=True, is_default=True).first()
            or qs.filter(is_default=True).first())


def _require(profile, field, key, label):
    account = getattr(profile, field, None)
    if account is None:
        raise MissingManufacturingAccount(key, f"No {label} account configured on the manufacturing profile.")
    return account


def _existing(tenant, ref_type, ref_id):
    from core.models import JournalEntry
    return JournalEntry.objects.filter(tenant=tenant, ref_type=ref_type, ref_id=str(ref_id)).first()


def _post(tenant, *, site, entry_date, ref_type, ref_id, memo, debit_account, credit_account,
          amount, user, debit_desc, credit_desc):
    """Create a balanced 2-line journal entry (idempotent on the reference)."""
    from core.models import JournalEntry, JournalLine
    existing = _existing(tenant, ref_type, ref_id)
    if existing is not None:
        return existing
    with transaction.atomic():
        je = JournalEntry.objects.create(
            tenant=tenant, site=site, entry_date=entry_date, ref_type=ref_type, ref_id=str(ref_id),
            memo=memo, posted_by=user, posted_at=timezone.now())
        JournalLine.objects.create(entry=je, account=debit_account, description=debit_desc,
                                   debit=amount, credit=ZERO)
        JournalLine.objects.create(entry=je, account=credit_account, description=credit_desc,
                                   debit=ZERO, credit=amount)
    return je


# --------------------------------------------------------------------------- #
# Material issue: DR WIP / CR Raw Material Inventory
# --------------------------------------------------------------------------- #
def post_work_order_material_issue(wom, movement, user):
    wo = wom.work_order
    tenant = wo.tenant
    profile = get_profile(tenant, wo.site)
    if profile is None:
        return None  # manufacturing GL not configured - skip (Phase 6 behaviour)

    amount = (-(movement.value or ZERO)) if (movement.value or ZERO) < ZERO else (movement.value or ZERO)
    amount = Decimal(amount).quantize(ZERO)
    if amount <= ZERO:
        return None

    existing = _existing(tenant, REF_ISSUE, movement.id)
    if existing is not None:
        return existing

    wip = _require(profile, "wip_account", "wip", "WIP")
    raw = _require(profile, "raw_material_inventory_account", "raw_material_inventory", "raw material inventory")
    je = _post(tenant, site=wo.site, entry_date=timezone.localdate(),
               ref_type=REF_ISSUE, ref_id=movement.id,
               memo=f"WO {wo.work_order_number} issue {wom.component.sku}",
               debit_account=wip, credit_account=raw, amount=amount, user=user,
               debit_desc="WIP - material issued", credit_desc=f"Raw material {wom.component.sku}")
    if movement.journal_entry_id is None:
        movement.journal_entry = je
        movement.save(update_fields=["journal_entry"])
    return je


# --------------------------------------------------------------------------- #
# Completion: DR Finished Goods Inventory / CR WIP
# --------------------------------------------------------------------------- #
def post_work_order_completion(wo, movement, user):
    tenant = wo.tenant
    profile = get_profile(tenant, wo.site)
    if profile is None:
        return None

    amount = Decimal(movement.value or ZERO).quantize(ZERO)
    if amount <= ZERO:
        return None

    existing = _existing(tenant, REF_COMPLETION, movement.id)
    if existing is not None:
        return existing

    fg = _require(profile, "finished_goods_inventory_account", "finished_goods_inventory", "finished goods inventory")
    wip = _require(profile, "wip_account", "wip", "WIP")
    je = _post(tenant, site=wo.site, entry_date=timezone.localdate(),
               ref_type=REF_COMPLETION, ref_id=movement.id,
               memo=f"WO {wo.work_order_number} completion {wo.product.sku}",
               debit_account=fg, credit_account=wip, amount=amount, user=user,
               debit_desc=f"Finished goods {wo.product.sku}", credit_desc="WIP - completed")
    if movement.journal_entry_id is None:
        movement.journal_entry = je
        movement.save(update_fields=["journal_entry"])
    return je


# --------------------------------------------------------------------------- #
# Close: clear remaining WIP to Manufacturing Variance
# --------------------------------------------------------------------------- #
def post_work_order_close_variance(wo, user):
    tenant = wo.tenant
    profile = get_profile(tenant, wo.site)
    if profile is None:
        if wo.variance_posted_at is None:
            wo.variance_posted_at = timezone.now()
            wo.save(update_fields=["variance_posted_at"])
        return None

    if wo.variance_journal_id is not None:
        return wo.variance_journal
    existing = _existing(tenant, REF_VARIANCE, wo.work_order_number)
    if existing is not None:
        return existing

    remaining = (wo.wip_material_cost or ZERO) - (wo.finished_goods_cost or ZERO)
    remaining = Decimal(remaining).quantize(ZERO)
    if remaining == ZERO:
        if wo.variance_posted_at is None:
            wo.variance_posted_at = timezone.now()
            wo.save(update_fields=["variance_posted_at"])
        return None

    wip = _require(profile, "wip_account", "wip", "WIP")
    variance = _require(profile, "manufacturing_variance_account", "manufacturing_variance", "manufacturing variance")
    if remaining > ZERO:
        # Material left in WIP that never became finished goods -> a cost (loss).
        debit_account, credit_account = variance, wip
        amount = remaining
        d_desc, c_desc = "Manufacturing variance", "WIP - cleared on close"
    else:
        # More credited out of WIP than was issued -> a gain.
        debit_account, credit_account = wip, variance
        amount = -remaining
        d_desc, c_desc = "WIP - cleared on close", "Manufacturing variance"

    je = _post(tenant, site=wo.site, entry_date=timezone.localdate(),
               ref_type=REF_VARIANCE, ref_id=wo.work_order_number,
               memo=f"WO {wo.work_order_number} WIP variance on close",
               debit_account=debit_account, credit_account=credit_account, amount=amount, user=user,
               debit_desc=d_desc, credit_desc=c_desc)
    wo.variance_journal = je
    wo.variance_posted_at = timezone.now()
    wo.save(update_fields=["variance_journal", "variance_posted_at"])
    return je


# --------------------------------------------------------------------------- #
# Corrections (Phase 12). Each is idempotent on the correction movement id and
# links the journal to that movement. Amounts come from the correction service
# (already non-negative). Scrap uses the manufacturing variance account when no
# dedicated scrap-expense account exists.
# --------------------------------------------------------------------------- #
def _post_correction(wo, movement, amount, user, *, ref_type, debit_field, debit_label,
                     credit_field, credit_label, memo, debit_desc, credit_desc):
    tenant = wo.tenant
    profile = get_profile(tenant, wo.site)
    if profile is None:
        return None
    amount = Decimal(amount or ZERO).quantize(ZERO)
    if amount <= ZERO:
        return None
    existing = _existing(tenant, ref_type, movement.id)
    if existing is not None:
        return existing
    debit_account = _require(profile, debit_field, debit_field, debit_label)
    credit_account = _require(profile, credit_field, credit_field, credit_label)
    je = _post(tenant, site=wo.site, entry_date=timezone.localdate(),
               ref_type=ref_type, ref_id=movement.id, memo=memo,
               debit_account=debit_account, credit_account=credit_account, amount=amount,
               user=user, debit_desc=debit_desc, credit_desc=credit_desc)
    if movement.journal_entry_id is None:
        movement.journal_entry = je
        movement.save(update_fields=["journal_entry"])
    return je


def post_work_order_material_scrap(wo, movement, amount, user):
    """Component scrap: DR Manufacturing Variance (scrap) / CR WIP."""
    return _post_correction(
        wo, movement, amount, user, ref_type=REF_SCRAP,
        debit_field="manufacturing_variance_account", debit_label="scrap / manufacturing variance",
        credit_field="wip_account", credit_label="WIP",
        memo=f"WO {wo.work_order_number} material scrap",
        debit_desc="Scrap / manufacturing variance", credit_desc="WIP - material scrapped")


def post_work_order_completion_scrap(wo, movement, amount, user):
    """Finished-goods scrap: DR Manufacturing Variance (scrap) / CR WIP."""
    return _post_correction(
        wo, movement, amount, user, ref_type=REF_COMPLETION_SCRAP,
        debit_field="manufacturing_variance_account", debit_label="scrap / manufacturing variance",
        credit_field="wip_account", credit_label="WIP",
        memo=f"WO {wo.work_order_number} finished-goods scrap",
        debit_desc="Scrap / manufacturing variance", credit_desc="WIP - finished goods scrapped")


def post_work_order_issue_reversal(wo, movement, amount, user):
    """Reverse a material issue: DR Raw Material Inventory / CR WIP."""
    return _post_correction(
        wo, movement, amount, user, ref_type=REF_ISSUE_REVERSAL,
        debit_field="raw_material_inventory_account", debit_label="raw material inventory",
        credit_field="wip_account", credit_label="WIP",
        memo=f"WO {wo.work_order_number} issue reversal",
        debit_desc="Raw material returned", credit_desc="WIP - issue reversed")


def post_work_order_completion_reversal(wo, movement, amount, user):
    """Reverse a completion: DR WIP / CR Finished Goods Inventory."""
    return _post_correction(
        wo, movement, amount, user, ref_type=REF_COMPLETION_REVERSAL,
        debit_field="wip_account", debit_label="WIP",
        credit_field="finished_goods_inventory_account", credit_label="finished goods inventory",
        memo=f"WO {wo.work_order_number} completion reversal",
        debit_desc="WIP - completion reversed", credit_desc=f"Finished goods {wo.product.sku}")
