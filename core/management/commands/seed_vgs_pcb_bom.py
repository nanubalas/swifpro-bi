"""Seed the VGS PCB electronics BOM (master data only).

Idempotent: re-running creates nothing new. Builds categories, a finished
product, its component products, a BOM header, BOM lines (total qty per
assembly) and reference-designator placement rows (R2, Z1..Z8, ...). R1 is
"open / not fitted", so it is recorded only as a header note - never as a
stock component or a material requirement.

Source: VGS and Technologies Pvt Ltd, PO No VGSP/MRT/26-27/041, 07-05-2026.

This touches BOM master data only - no inventory, costing, GL or posting.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import (
    Tenant, UnitOfMeasure, ProductCategory, Product,
    BillOfMaterials, BillOfMaterialsLine, BillOfMaterialsLinePlacement,
)

TENANT_NAME = "VGS and Technologies Pvt Ltd"
PARENT_SKU = "VGS-PCB-ASSY-001"
BOM_NAME = "VGS PCB Assembly BOM"
HEADER_NOTE = "R1 is open / not fitted. Source PO VGSP/MRT/26-27/041 dated 07-05-2026."

CATEGORIES = ["PCB Assemblies", "Resistors", "Zener Diodes", "Printed Circuit Boards", "LEDs"]

PARENT = dict(
    sku=PARENT_SKU, name="VGS PCB Assembly", category="PCB Assemblies",
    product_type="FINISHED_GOOD", brand="",
    description="PCB assembly based on PO VGSP/MRT/26-27/041 dated 07-05-2026. R1 is open / not fitted.",
)

COMPONENTS = [
    dict(sku="RES-820K-025W-1-TH", name="820K 0.25W 1% Resistor", category="Resistors",
         brand="Watts / Vishay / AVX / Royalohm", description="Value: 820K / 0.25W / 1%. Package: TH."),
    dict(sku="ZD-2V7-DO41-2", name="2V7 Zener Diode", category="Zener Diodes",
         brand="Vishay / MIC / ODIL", description="Value: 2V7 ZENER. Package: DO-41-2."),
    dict(sku="ZD-2V4-DO41-2", name="2V4 Zener Diode", category="Zener Diodes",
         brand="Vishay / MIC / ODIL", description="Value: 2V4 ZENER. Package: DO-41-2."),
    dict(sku="PCB-1P6MM-VGS", name="1.6mm PCB", category="Printed Circuit Boards",
         brand="", description="Value: 1.6MM PCB. Package: PCB."),
    dict(sku="LED-5MM-WHITE-NSPW500DS", name="5mm White LED", category="LEDs",
         brand="AVAGO / NICHIA",
         description="Value: NSPW500DS / LOT K9LP73-b1V / 5mm White LEDs. Package: 5mm LED."),
]

LINES = [
    dict(line_no=10, sku="RES-820K-025W-1-TH", qty=Decimal("1"), notes="R2", refs=["R2"]),
    dict(line_no=20, sku="ZD-2V7-DO41-2", qty=Decimal("8"), notes="Z1 to Z8",
         refs=[f"Z{i}" for i in range(1, 9)]),
    dict(line_no=30, sku="ZD-2V4-DO41-2", qty=Decimal("5"), notes="Z9 to Z13",
         refs=[f"Z{i}" for i in range(9, 14)]),
    dict(line_no=40, sku="PCB-1P6MM-VGS", qty=Decimal("1"), notes="PCB", refs=["PCB"]),
    dict(line_no=50, sku="LED-5MM-WHITE-NSPW500DS", qty=Decimal("13"), notes="L1 to L13",
         refs=[f"L{i}" for i in range(1, 14)]),
]


class Command(BaseCommand):
    help = "Seed the VGS PCB electronics BOM with reference-designator placements (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=TENANT_NAME,
                            help=f'Tenant name to seed into (default: "{TENANT_NAME}").')

    @transaction.atomic
    def handle(self, *args, **options):
        tenant, _ = Tenant.objects.get_or_create(name=options["tenant"])
        ea, _ = UnitOfMeasure.objects.get_or_create(
            tenant=tenant, code="EA", defaults={"name": "Each"})

        cats = {}
        for name in CATEGORIES:
            cats[name], _ = ProductCategory.objects.get_or_create(
                tenant=tenant, name=name, parent=None)

        def make_product(spec, product_type):
            return Product.objects.get_or_create(
                tenant=tenant, sku=spec["sku"],
                defaults={
                    "name": spec["name"],
                    "product_type": product_type,
                    "category": cats[spec["category"]],
                    "brand": spec["brand"],
                    "description": spec["description"],
                    "base_uom": ea,
                    "uom": "EA",
                    "is_active": True,
                    "track_serial": False,
                    "track_lots": False,
                    "track_expiry": False,
                },
            )

        parent, p_created = make_product(PARENT, "FINISHED_GOOD")
        comp_created = 0
        components = {}
        for spec in COMPONENTS:
            obj, created = make_product(spec, "STOCK")
            components[spec["sku"]] = obj
            comp_created += int(created)

        bom, _ = BillOfMaterials.objects.get_or_create(
            tenant=tenant, product=parent, name=BOM_NAME,
            defaults={"output_qty": Decimal("1"), "is_active": True, "notes": HEADER_NOTE},
        )
        # Keep the header note current even if the BOM already existed.
        if bom.notes != HEADER_NOTE or bom.output_qty != Decimal("1"):
            bom.notes = HEADER_NOTE
            bom.output_qty = Decimal("1")
            bom.save(update_fields=["notes", "output_qty"])

        line_count = placement_count = 0
        for spec in LINES:
            line, _ = BillOfMaterialsLine.objects.update_or_create(
                bom=bom, line_no=spec["line_no"],
                defaults={
                    "component": components[spec["sku"]],
                    "qty": spec["qty"],
                    "uom": ea,
                    "notes": spec["notes"],
                },
            )
            line_count += 1
            for ref in spec["refs"]:
                _, created = BillOfMaterialsLinePlacement.objects.get_or_create(
                    bom_line=line, reference=ref, defaults={"qty": Decimal("1")})
                placement_count += int(created)

        # R1 is explicitly NOT created as a component.
        assert not Product.objects.filter(tenant=tenant, sku="R1").exists()

        self.stdout.write(self.style.SUCCESS(
            f"VGS PCB BOM seeded into '{tenant.name}': parent {PARENT_SKU} "
            f"(new={p_created}), {len(COMPONENTS)} components (new={comp_created}), "
            f"{line_count} BOM lines, {placement_count} new placement(s). "
            f"R1 left open / not fitted (header note only)."))
