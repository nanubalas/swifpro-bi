"""Seed basic UOM data for a tenant so UOM conversion is usable/visible.

Idempotent — safe to run repeatedly; matches existing rows by tenant + code and
never duplicates. Default tenant: "SwifPro BI Ltd".

    python manage.py seed_uom_demo
    python manage.py seed_uom_demo --tenant "Other Co"

Seeds:
  * a base unit (reuses an existing each/EA unit if present, else creates "EA")
  * a "CASE" purchase/sales unit
  * a conversion rule: 1 CASE = 12 <base>
  * sets base_uom = the base unit on products that have none (so conversions are
    live for them; existing base_uom choices are left untouched)
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from core.models import Tenant, UnitOfMeasure, UOMConversion, Product


class Command(BaseCommand):
    help = "Seed EA/CASE units + a 1 CASE = 12 EA conversion for a tenant (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default="SwifPro BI Ltd", help="Tenant name (default 'SwifPro BI Ltd').")

    def handle(self, *args, **options):
        name = options["tenant"]
        tenant = Tenant.objects.filter(name=name).first()
        if tenant is None:
            raise CommandError(f"Tenant '{name}' not found.")

        # Base unit: reuse an existing each/EA-style unit to avoid a duplicate.
        base = (UnitOfMeasure.objects.filter(tenant=tenant, code__iexact="each").first()
                or UnitOfMeasure.objects.filter(tenant=tenant, code__iexact="ea").first())
        if base is None:
            base = UnitOfMeasure.objects.create(tenant=tenant, code="EA", name="Each")
            self.stdout.write(self.style.SUCCESS(f"Created base unit {base.code}"))
        else:
            self.stdout.write(f"Reusing existing base unit {base.code}")

        case, created = UnitOfMeasure.objects.get_or_create(
            tenant=tenant, code="CASE", defaults={"name": "Case"})
        self.stdout.write(self.style.SUCCESS("Created CASE") if created else "CASE already exists")

        conv, created = UOMConversion.objects.get_or_create(
            tenant=tenant, product=None, from_uom=case, to_uom=base,
            defaults={"multiplier": Decimal("12")})
        self.stdout.write(self.style.SUCCESS(f"Created conversion 1 {case.code} = {conv.multiplier} {base.code}")
                          if created else f"Conversion already exists (1 {case.code} = {conv.multiplier} {base.code})")

        # Default base unit for products that have none, so CASE conversions work.
        updated = Product.objects.filter(tenant=tenant, base_uom__isnull=True).update(base_uom=base)
        self.stdout.write(self.style.SUCCESS(
            f"Set base_uom={base.code} on {updated} product(s) that had none."))

        self.stdout.write(self.style.SUCCESS(
            f"UOM seed complete for '{name}'. Manage at /uoms/ and /uom-conversions/."))
