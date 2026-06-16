"""Configure manufacturing GL accounts + a default ManufacturingAccountingProfile
for a tenant, so work-order WIP posting (Phase 7) is active.

Idempotent: re-running reuses existing accounts and the existing default profile.

    python manage.py setup_manufacturing_gl --tenant "Acme"
"""
from django.core.management.base import BaseCommand, CommandError

from core.models import Tenant, GLAccount, ManufacturingAccountingProfile

ACCOUNTS = [
    ("1020", "Raw Material Inventory", "ASSET"),
    ("1030", "Work In Progress (WIP)", "ASSET"),
    ("1040", "Finished Goods Inventory", "ASSET"),
    ("5300", "Manufacturing Variance", "EXPENSE"),
    ("5400", "Direct Labour Absorption", "EXPENSE"),
    ("5500", "Manufacturing Overhead Absorption", "EXPENSE"),
]


class Command(BaseCommand):
    help = "Set up manufacturing GL accounts and a default accounting profile for a tenant."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant name or id.")

    def handle(self, *args, **opts):
        tenant = self._resolve_tenant(opts["tenant"])
        accounts = {}
        for code, name, acc_type in ACCOUNTS:
            acc, _ = GLAccount.objects.get_or_create(
                tenant=tenant, code=code, defaults={"name": name, "type": acc_type})
            accounts[code] = acc

        profile, created = ManufacturingAccountingProfile.objects.get_or_create(
            tenant=tenant, site=None,
            defaults={"is_default": True, "is_active": True})
        profile.raw_material_inventory_account = accounts["1020"]
        profile.wip_account = accounts["1030"]
        profile.finished_goods_inventory_account = accounts["1040"]
        profile.manufacturing_variance_account = accounts["5300"]
        profile.direct_labour_absorption_account = accounts["5400"]
        profile.manufacturing_overhead_absorption_account = accounts["5500"]
        profile.is_default = True
        profile.is_active = True
        profile.save()

        self.stdout.write(self.style.SUCCESS(
            f"Manufacturing GL {'created' if created else 'updated'} for {tenant.name}: "
            f"accounts 1020/1030/1040/5300/5400/5500 + default profile."))

    def _resolve_tenant(self, ref):
        if str(ref).isdigit():
            t = Tenant.objects.filter(id=int(ref)).first()
            if t:
                return t
        t = Tenant.objects.filter(name=ref).first()
        if not t:
            raise CommandError(f"No tenant matching '{ref}'.")
        return t
