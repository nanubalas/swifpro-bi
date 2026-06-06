"""Seed a 'UK' company with the Company -> Site -> Inventory Location structure
the spec calls for: city operating sites (each with named inventory locations)
plus region sites. Idempotent: safe to run repeatedly."""
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Tenant, Site, Location


# Site (city branch) -> its inventory locations [(name, location_type)]
CITY_SITES = {
    "London": [
        ("London Main Warehouse", Location.Type.WAREHOUSE),
        ("London Shop Floor", Location.Type.SHOP_FLOOR),
        ("London Cold Storage", Location.Type.COLD_STORAGE),
        ("London 3PL Warehouse", Location.Type.THREEPL),
    ],
    "Leicester": [
        ("Leicester Main Warehouse", Location.Type.WAREHOUSE),
        ("Leicester Shop Floor", Location.Type.SHOP_FLOOR),
        ("Leicester Back Room", Location.Type.BACK_ROOM),
        ("Leicester Returns Area", Location.Type.RETURNS),
        ("Leicester Delivery Van", Location.Type.VAN),
    ],
    "Manchester": [
        ("Manchester Main Warehouse", Location.Type.WAREHOUSE),
        ("Manchester Shop Floor", Location.Type.SHOP_FLOOR),
        ("Manchester Delivery Van", Location.Type.VAN),
    ],
    "Birmingham": [
        ("Birmingham Main Warehouse", Location.Type.WAREHOUSE),
        ("Birmingham Back Room", Location.Type.BACK_ROOM),
        ("Birmingham Damaged Goods Area", Location.Type.DAMAGED),
    ],
}

# Region sites (alternative taxonomy) - no inventory locations in this demo.
REGION_SITES = ["England", "Wales", "Scotland", "Northern Ireland"]


class Command(BaseCommand):
    help = "Seed the 'UK' company with city + region Sites and per-site inventory Locations."

    @transaction.atomic
    def handle(self, *args, **options):
        tenant, created = Tenant.objects.get_or_create(
            name="UK",
            defaults={"legal_name": "UK Operations Ltd", "currency_code": "GBP",
                      "country": "United Kingdom", "vat_registered": True},
        )
        self.stdout.write(("Created" if created else "Reusing") + f" company: {tenant.name}")
        # The post_save signal already made a default 'Main Site' + 'Main Location'.

        for city, locations in CITY_SITES.items():
            site, _ = Site.objects.get_or_create(
                tenant=tenant, name=city,
                defaults={"site_type": Site.Type.CITY_BRANCH, "region": city, "is_active": True},
            )
            for name, ltype in locations:
                Location.objects.get_or_create(
                    tenant=tenant, name=name,
                    defaults={"site": site, "type": ltype, "is_active": True, "holds_stock": True},
                )
            self.stdout.write(f"  Site '{city}': {len(locations)} inventory locations")

        for region in REGION_SITES:
            Site.objects.get_or_create(
                tenant=tenant, name=region,
                defaults={"site_type": Site.Type.REGION, "region": region, "is_active": True},
            )
        self.stdout.write(f"  Region sites: {', '.join(REGION_SITES)}")

        self.stdout.write(self.style.SUCCESS(
            f"UK demo ready: {Site.objects.filter(tenant=tenant).count()} sites, "
            f"{Location.objects.filter(tenant=tenant).count()} inventory locations."))
