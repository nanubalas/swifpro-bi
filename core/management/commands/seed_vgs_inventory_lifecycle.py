"""Seed a complete inventory-lifecycle master/support data set for VGS.

Builds the supporting metadata around the VGS PCB BOM so the company has a
usable inventory setup: one site (Main Site), inventory locations, advisory
bins, units of measure (+ example conversions), product categories, supportive
products (consumables/packaging/tools/QC), suppliers, a sample customer and
replenishment policies. The VGS BOM itself (products + lines + placements) is
ensured by delegating to `seed_vgs_pcb_bom`.

Master/support data only - it creates NO inventory postings, costing, GL, sales
or purchasing records. Opening stock is optional and only via the approved
inventory movement service, behind --with-opening-stock.

Idempotent: re-running creates nothing new.
"""
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.db import transaction

from core.models import (
    Tenant, OrgMembership, Site, Location, Bin, UnitOfMeasure, UOMConversion,
    ProductCategory, Product, Supplier, Customer, ReplenishmentPolicy,
    InventoryMovement,
)

TENANT_NAME = "VGS and Technologies Pvt Ltd"
SITE_NAME = "Main Site"

# (name, Location.Type, holds_stock)
LOCATIONS = [
    ("Raw Material Stores", "WAREHOUSE", True),
    ("ESD Component Store", "WAREHOUSE", True),
    ("PCB Store", "WAREHOUSE", True),
    ("Production Floor", "SHOP_FLOOR", True),
    ("Work In Progress Area", "STORAGE", True),
    ("QC Hold", "QUARANTINE", True),
    ("Quarantine", "QUARANTINE", True),
    ("Finished Goods", "WAREHOUSE", True),
    ("Scrap Location", "DAMAGED", True),
    ("In Transit", "TRANSIT", True),
]

# location name -> [bin codes]
BINS = {
    "Raw Material Stores": ["RMS-RACK-A-01", "RMS-RACK-A-02", "RMS-RACK-B-01"],
    "ESD Component Store": ["ESD-ZENER-A", "ESD-ZENER-B", "ESD-LED-A", "ESD-RES-A"],
    "PCB Store": ["PCB-RACK-01", "PCB-RACK-02"],
    "Production Floor": ["PROD-LINE-01", "PROD-LINE-02"],
    "QC Hold": ["QC-HOLD-01"],
    "Quarantine": ["QTN-01"],
    "Finished Goods": ["FG-RACK-01", "FG-RACK-02"],
    "Scrap Location": ["SCRAP-01"],
}

UOMS = [
    ("EA", "Each"), ("PCS", "Pieces"), ("PACK", "Pack"),
    ("REEL", "Reel"), ("TRAY", "Tray"), ("BAG", "Bag"),
]
# (from_code, to_code, multiplier) - tenant-level (product=None) examples.
CONVERSIONS = [
    ("PACK", "EA", Decimal("100")),   # 1 PACK of LEDs = 100 EA
    ("BAG", "EA", Decimal("100")),    # 1 BAG of Zeners = 100 EA
    ("REEL", "EA", Decimal("5000")),  # 1 REEL of Resistors = 5000 EA
    ("TRAY", "EA", Decimal("50")),    # 1 TRAY of PCB Assemblies = 50 EA
]

CATEGORIES = [
    "PCB Assemblies", "Resistors", "Zener Diodes", "Printed Circuit Boards", "LEDs",
    "Consumables", "Packaging Materials", "Tools and Fixtures",
    "Quality Inspection Items", "Scrap and Rework",
]

SUPPLIERS = [
    ("Vishay Components Supplier", "Resistors, Zener Diodes"),
    ("LED Components Supplier", "5mm White LEDs"),
    ("PCB Fabrication Supplier", "Bare PCBs"),
    ("Local Electronics Supplier", "Consumables, flux, solder, IPA"),
    ("Packaging Supplier", "ESD bags, cartons"),
]


class Command(BaseCommand):
    help = "Seed VGS inventory-lifecycle master/support data (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=TENANT_NAME)
        parser.add_argument("--with-opening-stock", action="store_true",
                            help="Also post example opening stock via the approved movement service.")

    @transaction.atomic
    def handle(self, *args, **options):
        tenant, _ = Tenant.objects.get_or_create(name=options["tenant"])

        # Visibility: every superuser gets non-default ADMIN access to the company.
        for su in User.objects.filter(is_superuser=True):
            OrgMembership.objects.get_or_create(
                user=su, tenant=tenant, defaults={"role": "ADMIN", "is_default": False})

        # Ensure the BOM, its products and placements exist (idempotent).
        call_command("seed_vgs_pcb_bom", tenant=tenant.name)

        # --- Single site: Main Site only ---
        site, _ = Site.objects.get_or_create(
            tenant=tenant, name=SITE_NAME,
            defaults={"code": "MAIN", "site_type": Site.Type.OPERATING_SITE,
                      "is_active": True, "is_default": True})

        # --- Locations under Main Site ---
        locs = {}
        for name, ltype, holds in LOCATIONS:
            loc, _ = Location.objects.get_or_create(
                tenant=tenant, name=name,
                defaults={"site": site, "type": ltype, "is_active": True, "holds_stock": holds})
            if loc.site_id != site.id:           # keep it pinned to Main Site
                loc.site = site
                loc.save(update_fields=["site"])
            locs[name] = loc

        # --- Bins (advisory) ---
        bin_count = 0
        for loc_name, codes in BINS.items():
            for code in codes:
                _, created = Bin.objects.get_or_create(
                    tenant=tenant, location=locs[loc_name], code=code,
                    defaults={"is_active": True})
                bin_count += int(created)

        # --- Units of measure + example conversions ---
        uoms = {}
        for code, label in UOMS:
            uoms[code], _ = UnitOfMeasure.objects.get_or_create(
                tenant=tenant, code=code, defaults={"name": label})
        ea = uoms["EA"]
        for fr, to, mult in CONVERSIONS:
            UOMConversion.objects.get_or_create(
                tenant=tenant, product=None, from_uom=uoms[fr], to_uom=uoms[to],
                defaults={"multiplier": mult})

        # --- Categories ---
        cats = {}
        for name in CATEGORIES:
            cats[name], _ = ProductCategory.objects.get_or_create(
                tenant=tenant, name=name, parent=None)

        # --- Suppliers ---
        sup = {}
        for name, supplies in SUPPLIERS:
            sup[name], _ = Supplier.objects.get_or_create(
                tenant=tenant, name=name,
                defaults={"status": Supplier.Status.ACTIVE, "categories": supplies})

        # --- Supportive products ---
        def upsert(sku, name, category, product_type, supplier=None,
                   track_lots=False, track_expiry=False, track_serial=False, brand="", desc=""):
            obj, _ = Product.objects.get_or_create(
                tenant=tenant, sku=sku, defaults={"name": name, "product_type": product_type})
            obj.name = name
            obj.category = cats[category]
            obj.product_type = product_type
            obj.base_uom = ea
            obj.uom = "EA"
            obj.is_active = True
            obj.track_lots = track_lots
            obj.track_expiry = track_expiry
            obj.track_serial = track_serial
            if brand:
                obj.brand = brand
            if desc:
                obj.description = desc
            if supplier is not None:
                obj.preferred_supplier = supplier
            obj.save()
            return obj

        local = sup["Local Electronics Supplier"]
        pkg = sup["Packaging Supplier"]
        support = {}
        support["solder"] = upsert("SOLDER-WIRE-LEADFREE-0P8MM", "Lead Free Solder Wire 0.8mm",
                                   "Consumables", "STOCK", supplier=local, track_lots=True)
        support["flux"] = upsert("FLUX-NOCLEAN-100ML", "No Clean Flux 100ml",
                                 "Consumables", "STOCK", supplier=local, track_lots=True, track_expiry=True)
        support["ipa"] = upsert("IPA-CLEANER-500ML", "IPA Cleaner 500ml",
                                "Consumables", "STOCK", supplier=local, track_lots=True, track_expiry=True)
        support["esdbag"] = upsert("ESD-BAG-PCB-100X150", "ESD Bag for PCB Assembly",
                                   "Packaging Materials", "STOCK", supplier=pkg)
        support["carton"] = upsert("CARTON-VGS-PCB-50", "Carton for 50 PCB Assemblies",
                                   "Packaging Materials", "STOCK", supplier=pkg)
        upsert("FIXTURE-VGS-PCB-TEST-001", "VGS PCB Test Fixture",
               "Tools and Fixtures", "NON_STOCK", track_serial=True)
        upsert("QC-CHECKLIST-VGS-PCB", "VGS PCB QC Checklist",
               "Quality Inspection Items", "NON_STOCK")

        # --- BOM component metadata: link supplier, lot-track diodes/LEDs ---
        def comp(sku):
            return Product.objects.filter(tenant=tenant, sku=sku).first()

        meta = {
            "RES-820K-025W-1-TH": (sup["Vishay Components Supplier"], False, False),
            "ZD-2V7-DO41-2": (sup["Vishay Components Supplier"], True, False),
            "ZD-2V4-DO41-2": (sup["Vishay Components Supplier"], True, False),
            "PCB-1P6MM-VGS": (sup["PCB Fabrication Supplier"], False, False),
            "LED-5MM-WHITE-NSPW500DS": (sup["LED Components Supplier"], True, False),
            "VGS-PCB-ASSY-001": (None, False, False),
        }
        for sku, (supplier, lots, serial) in meta.items():
            p = comp(sku)
            if not p:
                continue
            p.base_uom = ea
            p.track_lots = lots
            p.track_serial = serial
            if supplier is not None:
                p.preferred_supplier = supplier
            p.save()

        # --- Replenishment policies (product, location, min, reorder, target, lead, supplier) ---
        policies = [
            ("ZD-2V7-DO41-2", "ESD Component Store", 1000, 2000, 6000, 10, "Vishay Components Supplier"),
            ("ZD-2V4-DO41-2", "ESD Component Store", 800, 1500, 4000, 10, "Vishay Components Supplier"),
            ("LED-5MM-WHITE-NSPW500DS", "ESD Component Store", 1500, 3000, 10000, 14, "LED Components Supplier"),
            ("RES-820K-025W-1-TH", "Raw Material Stores", 500, 1000, 5000, 7, "Vishay Components Supplier"),
            ("PCB-1P6MM-VGS", "PCB Store", 100, 250, 1000, 21, "PCB Fabrication Supplier"),
            ("VGS-PCB-ASSY-001", "Finished Goods", 50, 100, 500, 3, None),
        ]
        policy_count = 0
        for sku, loc_name, mn, rop, target, lead, supplier_name in policies:
            p = comp(sku)
            if not p:
                continue
            _, created = ReplenishmentPolicy.objects.get_or_create(
                tenant=tenant, product=p, location=locs[loc_name],
                defaults={
                    "min_stock": Decimal(mn), "reorder_point": Decimal(rop),
                    "max_stock": Decimal(target),
                    "reorder_quantity": Decimal(target) - Decimal(rop),
                    "lead_time_days": lead, "is_active": True,
                    "preferred_supplier": sup.get(supplier_name) if supplier_name else None,
                })
            policy_count += int(created)

        # --- Sample customer ---
        Customer.objects.get_or_create(
            tenant=tenant, name="VGS Demo Customer",
            defaults={"status": Customer.Status.ACTIVE})

        opening_msg = "opening stock skipped (use --with-opening-stock)"
        if options["with_opening_stock"]:
            opening_msg = self._post_opening_stock(tenant, locs)

        self.stdout.write(self.style.SUCCESS(
            f"VGS inventory lifecycle seeded into '{tenant.name}': site '{SITE_NAME}', "
            f"{len(LOCATIONS)} locations, {bin_count} new bins, {len(UOMS)} UOMs, "
            f"{len(CATEGORIES)} categories, {len(SUPPLIERS)} suppliers, "
            f"{policy_count} new replenishment policies. {opening_msg}. "
            f"Switch company to '{tenant.name}' -> '{SITE_NAME}' to view."))

    def _post_opening_stock(self, tenant, locs):
        """Optional example opening stock via the approved apply_movement service
        (subledger movement only - no GL). Idempotent per product via ref_type."""
        from core.services.inventory import apply_movement
        plan = [
            ("RES-820K-025W-1-TH", "Raw Material Stores", Decimal("1000")),
            ("ZD-2V7-DO41-2", "ESD Component Store", Decimal("6000")),
            ("ZD-2V4-DO41-2", "ESD Component Store", Decimal("4000")),
            ("PCB-1P6MM-VGS", "PCB Store", Decimal("750")),
            ("LED-5MM-WHITE-NSPW500DS", "ESD Component Store", Decimal("10000")),
            ("ESD-BAG-PCB-100X150", "Raw Material Stores", Decimal("1000")),
            ("CARTON-VGS-PCB-50", "Raw Material Stores", Decimal("100")),
        ]
        posted = 0
        for sku, loc_name, qty in plan:
            p = Product.objects.filter(tenant=tenant, sku=sku).first()
            if not p or qty <= 0:
                continue
            ref_id = f"VGS-OPEN-{sku}"
            if InventoryMovement.objects.filter(tenant=tenant, product=p, ref_type="OPENING", ref_id=ref_id).exists():
                continue  # already posted - idempotent
            apply_movement(
                tenant=tenant, product=p, location=locs[loc_name],
                movement_type="RECEIVE", qty_delta=qty,
                ref_type="OPENING", ref_id=ref_id, unit_cost=Decimal("0"),
                lot_code=("OPEN" if p.track_lots else None),
                notes="Opening stock (seed)")
            posted += 1
        return f"opening stock posted for {posted} product(s)"
