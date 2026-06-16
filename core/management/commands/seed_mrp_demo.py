"""Seed a complete, realistic MRP demo scenario for a tenant (Phase 18).

    python manage.py seed_mrp_demo --tenant "<tenant name>"

Idempotent: re-running reuses existing records and never double-posts stock.
Creates sites/locations, products, suppliers, BOMs, routings + work centres + a
shop calendar, item planning profiles (BUY / MAKE / TRANSFER / SUBCONTRACT),
opening stock (incl. a quarantine and a transfer-site example), an active
forecast version + line, a confirmed sales order, a manufacturing GL profile,
and a DRAFT MRP run ready to run. No new business logic - it only uses existing
models and the inventory ledger service.
"""
import datetime
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import (
    Tenant, Site, Location, Product, Supplier, Customer, GLAccount,
    BillOfMaterials, BillOfMaterialsLine, WorkCentre, RoutingHeader, RoutingOperation,
    ShopCalendar, ShopCalendarWorkingDay, ShopCalendarException, ItemSitePlanning,
    ForecastVersion, ForecastLine, CustomerOrder, CustomerOrderLine, MRPRun,
    ManufacturingAccountingProfile, InventoryMovement, InventoryBalance,
)

SEED_REF = "MRP_DEMO_SEED"


class Command(BaseCommand):
    help = "Seed a complete MRP demo scenario for a tenant."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant name (created if missing).")

    @transaction.atomic
    def handle(self, *args, **opts):
        tenant = self._tenant(opts["tenant"])
        today = timezone.localdate()
        self.month_day = today.replace(day=15)

        sites = self._sites(tenant)
        locs = self._locations(tenant, sites)
        suppliers = self._suppliers(tenant)
        products = self._products(tenant)
        self._boms(tenant, products)
        cal = self._calendar(tenant)
        wcs = self._work_centres(tenant, sites, cal)
        self._routing(tenant, products, wcs, suppliers)
        self._planning(tenant, sites, products, suppliers)
        self._stock(tenant, products, locs)
        version = self._forecast(tenant, sites, products)
        self._sales(tenant, sites, products)
        self._gl(tenant)
        run = self._run(tenant, version)

        self.stdout.write(self.style.SUCCESS(
            f"MRP demo seeded for '{tenant.name}'. Open Planning -> MRP Runs -> {run.run_number} "
            f"and click Run MRP (BUY / MAKE / TRANSFER / SUBCONTRACT planned orders, forecast "
            f"consumption, pegging, capacity and reports will populate)."))

    # ------------------------------------------------------------------ #
    def _tenant(self, ref):
        t = Tenant.objects.filter(name=ref).first()
        if t is None and str(ref).isdigit():
            t = Tenant.objects.filter(id=int(ref)).first()
        if t is None:
            t = Tenant.objects.create(name=ref)  # signal seeds accounts + default site/location
        return t

    def _sites(self, tenant):
        main = Site.objects.filter(tenant=tenant).order_by("id").first()
        if main is None:
            main = Site.objects.create(tenant=tenant, name="Main Site",
                                       site_type=Site.Type.OPERATING_SITE, is_default=True)
        depot, _ = Site.objects.get_or_create(
            tenant=tenant, name="Transfer Depot",
            defaults={"site_type": Site.Type.OPERATING_SITE, "is_active": True})
        return {"main": main, "depot": depot}

    def _locations(self, tenant, sites):
        def loc(site, name, ltype, holds=True):
            obj, _ = Location.objects.get_or_create(
                tenant=tenant, site=site, name=name,
                defaults={"type": ltype, "holds_stock": holds, "is_active": True})
            return obj
        main = sites["main"]
        main_store = (Location.objects.filter(tenant=tenant, site=main, holds_stock=True,
                                              type="WAREHOUSE").order_by("id").first()
                      or loc(main, "Main Location", "WAREHOUSE"))
        return {
            "main_store": main_store,
            "fg": loc(main, "Finished Goods", "WAREHOUSE"),
            "quarantine": loc(main, "Quarantine", "QUARANTINE"),
            "transit": loc(main, "In Transit", "TRANSIT"),
            "depot_store": loc(sites["depot"], "Depot Store", "WAREHOUSE"),
        }

    def _suppliers(self, tenant):
        def sup(name):
            return Supplier.objects.get_or_create(tenant=tenant, name=name)[0]
        return {"raw": sup("Raw Materials Co"), "pkg": sup("Packaging Co"),
                "coat": sup("Coating Subcontractor")}

    def _products(self, tenant):
        specs = [
            ("FG-ALPHA", "Alpha finished good", "FINISHED_GOOD", "20.00"),
            ("SUB-ALPHA", "Alpha sub-assembly", "STOCK", "9.00"),
            ("RM-BOARD", "Control board", "RAW_MATERIAL", "6.00"),
            ("RM-SCREW", "M3 screw", "RAW_MATERIAL", "0.10"),
            ("PKG-BOX", "Shipping box", "STOCK", "1.20"),
            ("COAT-SERVICE", "Protective coating service", "SERVICE", "3.00"),
        ]
        out = {}
        for sku, name, ptype, cost in specs:
            out[sku] = Product.objects.get_or_create(
                tenant=tenant, sku=sku,
                defaults={"name": name, "product_type": ptype, "standard_cost": Decimal(cost),
                          "cost_method": "AVERAGE", "is_active": True})[0]
        return out

    def _boms(self, tenant, p):
        def bom(parent):
            return BillOfMaterials.objects.get_or_create(
                tenant=tenant, product=parent, name="Default BOM",
                defaults={"output_qty": Decimal("1"), "is_active": True})[0]
        def line(b, no, comp, qty, scrap="0", fixed="0"):
            BillOfMaterialsLine.objects.get_or_create(
                bom=b, component=comp,
                defaults={"line_no": no, "qty": Decimal(qty), "scrap_percent": Decimal(scrap),
                          "fixed_qty": Decimal(fixed)})
        fg = bom(p["FG-ALPHA"])
        line(fg, 10, p["SUB-ALPHA"], "1")
        line(fg, 20, p["RM-SCREW"], "4", scrap="5")        # scrap-percent example
        line(fg, 30, p["PKG-BOX"], "1", fixed="0")
        sub = bom(p["SUB-ALPHA"])
        line(sub, 10, p["RM-BOARD"], "1", fixed="1")        # fixed-qty example

    def _calendar(self, tenant):
        cal, created = ShopCalendar.objects.get_or_create(
            tenant=tenant, code="STD", defaults={"name": "Standard Week", "is_default": True})
        if created:
            for wd in range(7):
                working = wd < 5
                ShopCalendarWorkingDay.objects.create(
                    calendar=cal, weekday=wd, is_working_day=working,
                    start_time=datetime.time(9, 0) if working else None,
                    end_time=datetime.time(17, 0) if working else None,
                    capacity_multiplier=Decimal("1.00"))
            ShopCalendarException.objects.get_or_create(
                calendar=cal, date=self.month_day,
                defaults={"exception_type": "HOLIDAY", "capacity_multiplier": Decimal("0.00"),
                          "reason": "Demo holiday"})
        return cal

    def _work_centres(self, tenant, sites, cal):
        def wc(code, name, labour, overhead, finite):
            return WorkCentre.objects.get_or_create(
                tenant=tenant, code=code,
                defaults={"site": sites["main"], "name": name,
                          "capacity_hours_per_day": Decimal("8"), "efficiency_percent": Decimal("100"),
                          "labour_rate_per_hour": Decimal(labour), "overhead_rate_per_hour": Decimal(overhead),
                          "finite_capacity_enabled": finite, "shop_calendar": cal, "is_active": True})[0]
        return {"asm": wc("ASM", "Assembly", "25", "12", True),
                "test": wc("TEST", "Test", "20", "8", False)}

    def _routing(self, tenant, p, wcs, suppliers):
        routing, created = RoutingHeader.objects.get_or_create(
            tenant=tenant, routing_code="RT-FG-ALPHA",
            defaults={"product": p["FG-ALPHA"], "site": None, "status": "ACTIVE", "is_default": True})
        if created:
            RoutingOperation.objects.create(
                routing=routing, operation_sequence=10, operation_name="Assembly",
                work_centre=wcs["asm"], setup_minutes=Decimal("60"), run_minutes_per_unit=Decimal("30"))
            RoutingOperation.objects.create(
                routing=routing, operation_sequence=20, operation_name="Test",
                work_centre=wcs["test"], setup_minutes=Decimal("30"), run_minutes_per_unit=Decimal("15"))

    def _planning(self, tenant, sites, p, suppliers):
        def isp(product, site, source, **kw):
            defaults = dict(source_type=source, mrp_enabled=True, is_active=True,
                            safety_stock_qty=Decimal("0"), min_order_qty=Decimal("0"),
                            max_order_qty=Decimal("0"), order_multiple=Decimal("0"),
                            lead_time_days={"BUY": 7, "MAKE": 3, "TRANSFER": 2, "SUBCONTRACT": 10}.get(source, 0))
            defaults.update(kw)
            ItemSitePlanning.objects.get_or_create(
                tenant=tenant, product=product, site=site, defaults=defaults)
        main = sites["main"]
        isp(p["FG-ALPHA"], main, "MAKE", include_forecast=True)
        isp(p["SUB-ALPHA"], main, "MAKE")
        isp(p["RM-BOARD"], main, "TRANSFER", default_transfer_from_site=sites["depot"])
        isp(p["RM-SCREW"], main, "BUY", default_supplier=suppliers["raw"], min_order_qty=Decimal("100"),
            order_multiple=Decimal("50"))
        isp(p["PKG-BOX"], main, "BUY", default_supplier=suppliers["pkg"])
        isp(p["COAT-SERVICE"], main, "SUBCONTRACT", default_supplier=suppliers["coat"],
            safety_stock_qty=Decimal("15"))
        # Source the board from the depot itself (BUY at the depot).
        isp(p["RM-BOARD"], sites["depot"], "BUY", default_supplier=suppliers["raw"])

    def _receive(self, tenant, product, location, qty, cost, user=None):
        if InventoryMovement.objects.filter(tenant=tenant, ref_type=SEED_REF,
                                            product=product, location=location).exists():
            return
        from core.services.inventory import apply_movement
        apply_movement(tenant=tenant, product=product, location=location, movement_type="RECEIVE",
                       qty_delta=Decimal(qty), ref_type=SEED_REF, ref_id="opening",
                       unit_cost=Decimal(cost), user=user)

    def _stock(self, tenant, p, locs):
        # Quarantine board (must be EXCLUDED by MRP), some screws short (none),
        # packaging on hand, boards available at the depot for the transfer.
        self._receive(tenant, p["RM-BOARD"], locs["quarantine"], "20", "6.00")
        self._receive(tenant, p["PKG-BOX"], locs["main_store"], "5", "1.20")
        self._receive(tenant, p["RM-BOARD"], locs["depot_store"], "60", "6.00")
        self._receive(tenant, p["SUB-ALPHA"], locs["main_store"], "2", "9.00")

    def _forecast(self, tenant, sites, p):
        version, _ = ForecastVersion.objects.get_or_create(
            tenant=tenant, code="DEMO-FC",
            defaults={"name": "Demo Baseline", "status": "ACTIVE", "consumption_method": "SAME_BUCKET",
                      "is_default": True, "start_date": self.month_day.replace(day=1),
                      "end_date": (self.month_day.replace(day=28))})
        ForecastLine.objects.get_or_create(
            tenant=tenant, forecast_version=version, product=p["FG-ALPHA"], site=sites["main"],
            forecast_date=self.month_day,
            defaults={"bucket_type": "MONTHLY", "quantity": Decimal("30"),
                      "remaining_quantity": Decimal("30")})
        return version

    def _sales(self, tenant, sites, p):
        cust, _ = Customer.objects.get_or_create(tenant=tenant, name="Demo Customer")
        if not CustomerOrder.objects.filter(tenant=tenant, order_number="SO-DEMO-1").exists():
            o = CustomerOrder.objects.create(
                tenant=tenant, customer=cust, site=sites["main"], order_number="SO-DEMO-1",
                status="CONFIRMED", order_date=self.month_day)
            CustomerOrderLine.objects.create(order=o, product=p["FG-ALPHA"], qty=Decimal("10"))

    def _gl(self, tenant):
        def acc(code, name, atype):
            return GLAccount.objects.get_or_create(
                tenant=tenant, code=code, defaults={"name": name, "type": atype})[0]
        profile, _ = ManufacturingAccountingProfile.objects.get_or_create(
            tenant=tenant, site=None, defaults={"is_default": True, "is_active": True})
        profile.raw_material_inventory_account = acc("1020", "Raw Material Inventory", "ASSET")
        profile.wip_account = acc("1030", "Work In Progress (WIP)", "ASSET")
        profile.finished_goods_inventory_account = acc("1040", "Finished Goods Inventory", "ASSET")
        profile.manufacturing_variance_account = acc("5300", "Manufacturing Variance", "EXPENSE")
        profile.direct_labour_absorption_account = acc("5400", "Direct Labour Absorption", "EXPENSE")
        profile.manufacturing_overhead_absorption_account = acc("5500", "Manufacturing Overhead Absorption", "EXPENSE")
        profile.is_default = True
        profile.is_active = True
        profile.save()

    def _run(self, tenant, version):
        from core.services.mrp import next_run_number
        run = MRPRun.objects.filter(tenant=tenant, run_number__startswith="MRP-DEMO").first()
        if run is None:
            run = MRPRun.objects.create(
                tenant=tenant, run_number="MRP-DEMO-" + next_run_number(tenant)[-6:],
                site_scope=None, planning_start_date=self.month_day.replace(day=1),
                planning_end_date=self.month_day.replace(day=1) + datetime.timedelta(days=120),
                include_forecast=True, forecast_version=str(version.id), status="DRAFT")
        return run
