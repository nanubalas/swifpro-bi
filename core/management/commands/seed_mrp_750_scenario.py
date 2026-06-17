"""Seed and (optionally) run the FG-750-DEMO material-planning test scenario.

    python manage.py seed_mrp_750_scenario --tenant "MRP 750 Demo"
    python manage.py seed_mrp_750_scenario --tenant "MRP 750 Demo" --run

Builds a realistic single-level MAKE scenario for manual MRP testing:
- 1 finished good FG-750-DEMO with a 5-component BOM (RM-A..RM-E).
- A confirmed sales order for 750 finished units.
- Per-component BUY planning rules (safety stock, MOQ, order multiple, lead time).
- Opening stock incl. reserved and quarantine quantities, plus open POs.

Idempotent: re-running reuses records and never double-posts stock. Uses only
existing models and the inventory ledger service - no MRP logic is changed.

With --run it executes the MRP run and prints expected (classic single-level
netting) vs actual engine output so divergences are obvious.
"""
import datetime
from decimal import Decimal, ROUND_CEILING

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import (
    Tenant, Site, Location, Product, Supplier, Customer,
    BillOfMaterials, BillOfMaterialsLine, ItemSitePlanning,
    CustomerOrder, CustomerOrderLine, MRPRun,
    PurchaseOrder, PurchaseOrderLine, InventoryBalance,
    WorkCentre, RoutingHeader, RoutingOperation,
)

SEED_REF = "MRP_750_SEED"
ZERO = Decimal("0.00")

# component_sku, bom_qty_per_fg, safety, moq, multiple, lead_days,
# on_hand (nettable, main store), reserved, quarantine, open_po
COMPONENTS = [
    ("RM-A", "2",   "500",  "500",  "100", 10, "900",  "100", "0",   "300"),
    ("RM-B", "1",   "300",  "100",  "50",   7, "1000", "200", "0",   "0"),
    ("RM-C", "4",   "1000", "1000", "500", 14, "2000", "0",   "200", "500"),
    ("RM-D", "0.5", "200",  "500",  "50",   7, "100",  "0",   "0",   "0"),
    ("RM-E", "1",   "250",  "250",  "50",   5, "900",  "0",   "0",   "0"),
]
FG_SKU = "FG-750-DEMO"
ORDER_QTY = Decimal("750")
FG_MAKE_LEAD = 3


def _round_up_to_multiple(qty, multiple):
    if multiple <= ZERO:
        return qty
    lots = (qty / multiple).to_integral_value(rounding=ROUND_CEILING)
    return multiple * lots


def _classic_planned(net, moq, multiple):
    """Classic single-level lot sizing: max(net,0) -> MOQ -> order multiple."""
    qty = net if net > ZERO else ZERO
    if qty <= ZERO:
        return ZERO
    if moq > ZERO and qty < moq:
        qty = moq
    qty = _round_up_to_multiple(qty, multiple)
    return qty


class Command(BaseCommand):
    help = "Seed (and optionally run) the FG-750-DEMO 5-component MRP test scenario."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant name (created if missing).")
        parser.add_argument("--run", action="store_true", help="Execute the MRP run and print actual vs expected.")

    @transaction.atomic
    def handle(self, *args, **opts):
        tenant = self._tenant(opts["tenant"])
        self.today = timezone.localdate()
        # Sales order required well in the future so every release date is in the
        # future (no past-due noise): SO date - FG lead - longest component lead.
        self.so_required = self.today + datetime.timedelta(days=30)

        site = self._site(tenant)
        locs = self._locations(tenant, site)
        suppliers = self._suppliers(tenant)
        products = self._products(tenant)
        self._bom(tenant, products)
        self._routing(tenant, site, products)
        self._planning(tenant, site, products, suppliers)
        self._stock(tenant, products, locs)
        self._open_pos(tenant, site, products, suppliers, locs)
        self._sales(tenant, site, products)
        run = self._run(tenant, site)

        self._print_expected(site)
        self.stdout.write(self.style.SUCCESS(
            f"\nSeeded into '{tenant.name}'. MRP run: {run.run_number} (status {run.status}).\n"
            f"Open Planning -> MRP Runs -> {run.run_number} and click Run MRP, or re-run this "
            f"command with --run."))

        if opts["run"]:
            self._run_and_compare(run, site)

    # ------------------------------------------------------------------ #
    def _tenant(self, ref):
        t = Tenant.objects.filter(name=ref).first()
        if t is None and str(ref).isdigit():
            t = Tenant.objects.filter(id=int(ref)).first()
        if t is None:
            t = Tenant.objects.create(name=ref)  # signal seeds accounts + default site/location
        return t

    def _site(self, tenant):
        site = Site.objects.filter(tenant=tenant).order_by("id").first()
        if site is None:
            site = Site.objects.create(tenant=tenant, name="Main Site",
                                       site_type=Site.Type.OPERATING_SITE, is_default=True)
        return site

    def _locations(self, tenant, site):
        def loc(name, ltype):
            return Location.objects.get_or_create(
                tenant=tenant, site=site, name=name,
                defaults={"type": ltype, "holds_stock": True, "is_active": True})[0]
        store = (Location.objects.filter(tenant=tenant, site=site, holds_stock=True, type="WAREHOUSE")
                 .order_by("id").first() or loc("Main Location", "WAREHOUSE"))
        return {"store": store, "quarantine": loc("Quarantine", "QUARANTINE")}

    def _suppliers(self, tenant):
        return {c[0]: Supplier.objects.get_or_create(tenant=tenant, name=f"Supplier {c[0]}")[0]
                for c in COMPONENTS}

    def _products(self, tenant):
        out = {}
        out[FG_SKU] = Product.objects.get_or_create(
            tenant=tenant, sku=FG_SKU,
            defaults={"name": "Finished good (750 demo)", "product_type": "FINISHED_GOOD",
                      "standard_cost": Decimal("50.00"), "cost_method": "AVERAGE", "is_active": True})[0]
        for sku, *_rest in COMPONENTS:
            out[sku] = Product.objects.get_or_create(
                tenant=tenant, sku=sku,
                defaults={"name": f"Raw material {sku}", "product_type": "RAW_MATERIAL",
                          "standard_cost": Decimal("2.00"), "cost_method": "AVERAGE", "is_active": True})[0]
        return out

    def _bom(self, tenant, p):
        bom = BillOfMaterials.objects.get_or_create(
            tenant=tenant, product=p[FG_SKU], name="Default BOM",
            defaults={"output_qty": Decimal("1"), "is_active": True})[0]
        for i, (sku, qty, *_rest) in enumerate(COMPONENTS, start=1):
            BillOfMaterialsLine.objects.get_or_create(
                bom=bom, component=p[sku],
                defaults={"line_no": i * 10, "qty": Decimal(qty),
                          "scrap_percent": ZERO, "fixed_qty": ZERO})

    def _routing(self, tenant, site, p):
        # A simple single-operation routing so FG-750-DEMO (a MAKE item) has an
        # active routing and the run does not raise MISSING_ROUTING. No shop
        # calendar / finite scheduling - keeps the focus on material planning.
        wc = WorkCentre.objects.get_or_create(
            tenant=tenant, code="ASSY",
            defaults={"site": site, "name": "Assembly", "capacity_hours_per_day": Decimal("8"),
                      "efficiency_percent": Decimal("100"), "finite_capacity_enabled": False,
                      "is_active": True})[0]
        routing, created = RoutingHeader.objects.get_or_create(
            tenant=tenant, routing_code="RT-FG-750",
            defaults={"product": p[FG_SKU], "site": None, "status": "ACTIVE", "is_default": True})
        if created:
            # Small per-unit time so 750 units fit one shift (no capacity overload).
            RoutingOperation.objects.create(
                routing=routing, operation_sequence=10, operation_name="Assemble",
                work_centre=wc, setup_minutes=Decimal("10"), run_minutes_per_unit=Decimal("0.1"))

    def _planning(self, tenant, site, p, suppliers):
        # FG: MAKE, driven by the sales order.
        ItemSitePlanning.objects.get_or_create(
            tenant=tenant, product=p[FG_SKU], site=site,
            defaults=dict(source_type="MAKE", mrp_enabled=True, is_active=True,
                          include_sales_orders=True, include_forecast=False, include_safety_stock=True,
                          safety_stock_qty=ZERO, lead_time_days=FG_MAKE_LEAD,
                          lot_sizing_method="LOT_FOR_LOT"))
        for sku, _qty, safety, moq, mult, lead, *_rest in COMPONENTS:
            ItemSitePlanning.objects.get_or_create(
                tenant=tenant, product=p[sku], site=site,
                defaults=dict(source_type="BUY", mrp_enabled=True, is_active=True,
                              include_sales_orders=True, include_forecast=False, include_safety_stock=True,
                              default_supplier=suppliers[sku],
                              safety_stock_qty=Decimal(safety), min_order_qty=Decimal(moq),
                              order_multiple=Decimal(mult), lead_time_days=lead,
                              lot_sizing_method="LOT_FOR_LOT"))

    def _receive(self, tenant, product, location, qty, ref):
        if Decimal(qty) <= ZERO:
            return
        if InventoryMovement_exists(tenant, product, location, ref):
            return
        from core.services.inventory import apply_movement
        apply_movement(tenant=tenant, product=product, location=location, movement_type="RECEIVE",
                       qty_delta=Decimal(qty), ref_type=SEED_REF, ref_id=ref,
                       unit_cost=Decimal("2.00"))

    def _stock(self, tenant, p, locs):
        for sku, _qty, _safety, _moq, _mult, _lead, on_hand, reserved, quar, _po in COMPONENTS:
            self._receive(tenant, p[sku], locs["store"], on_hand, f"{sku}-onhand")
            self._receive(tenant, p[sku], locs["quarantine"], quar, f"{sku}-quar")
            # Reserved is set on the balance directly (idempotent: assignment, not increment).
            if Decimal(reserved) > ZERO:
                bal = InventoryBalance.objects.filter(
                    tenant=tenant, product=p[sku], location=locs["store"]).first()
                if bal and bal.reserved != Decimal(reserved):
                    bal.reserved = Decimal(reserved)
                    bal.save(update_fields=["reserved"])

    def _open_pos(self, tenant, site, p, suppliers, locs):
        expected = self.today + datetime.timedelta(days=5)  # due before production
        for sku, _qty, _safety, _moq, _mult, _lead, _oh, _res, _quar, po_qty in COMPONENTS:
            if Decimal(po_qty) <= ZERO:
                continue
            number = f"PO-750-{sku}"
            if PurchaseOrder.objects.filter(tenant=tenant, po_number=number).exists():
                continue
            po = PurchaseOrder.objects.create(
                tenant=tenant, po_number=number, supplier=suppliers[sku], site=site,
                receiving_location=locs["store"], status="APPROVED", is_current=True,
                expected_date=expected)
            PurchaseOrderLine.objects.create(
                po=po, product=p[sku], ordered_qty=Decimal(po_qty), received_qty=ZERO,
                unit_cost=Decimal("2.00"))

    def _sales(self, tenant, site, p):
        cust = Customer.objects.get_or_create(tenant=tenant, name="Demo Customer 750")[0]
        if not CustomerOrder.objects.filter(tenant=tenant, order_number="SO-750-1").exists():
            o = CustomerOrder.objects.create(
                tenant=tenant, customer=cust, site=site, order_number="SO-750-1",
                status="CONFIRMED", order_date=self.so_required)
            CustomerOrderLine.objects.create(order=o, product=p[FG_SKU], qty=ORDER_QTY)

    def _run(self, tenant, site):
        from core.services.mrp import next_run_number
        run = MRPRun.objects.filter(tenant=tenant, run_number__startswith="MRP-750").first()
        if run is None:
            run = MRPRun.objects.create(
                tenant=tenant, run_number="MRP-750-" + next_run_number(tenant)[-6:],
                site_scope=site, planning_start_date=self.today,
                planning_end_date=self.today + datetime.timedelta(days=180),
                include_sales_orders=True, include_forecast=False, include_safety_stock=True,
                include_transfers=True, status="DRAFT")
        return run

    # ------------------------------------------------------------------ #
    def _print_expected(self, site):
        fg_release = self.so_required - datetime.timedelta(days=FG_MAKE_LEAD)
        self.stdout.write("\nExpected (classic single-level netting incl. safety stock):")
        self.stdout.write(
            "  Comp  Qty/FG  Gross  Safety  OnHand  Resv  Quar  OpenPO  Usable  Net   MOQ   Mult  Planned  Release")
        for sku, qty, safety, moq, mult, lead, oh, res, quar, po in COMPONENTS:
            gross = ORDER_QTY * Decimal(qty)
            nettable = Decimal(oh) - Decimal(res)              # quarantine fully excluded
            usable = nettable + Decimal(po)
            net = gross + Decimal(safety) - usable
            planned = _classic_planned(net, Decimal(moq), Decimal(mult))
            release = fg_release - datetime.timedelta(days=lead)
            self.stdout.write(
                f"  {sku:5} {qty:>5}  {gross:>5}  {safety:>5}  {oh:>5}  {res:>4}  {quar:>4}  "
                f"{po:>5}  {usable:>6}  {net:>4}  {moq:>4}  {mult:>4}  {planned:>7}  {release}")
        self.stdout.write(f"\n  FG {FG_SKU}: MAKE {ORDER_QTY}, required {self.so_required}, release {fg_release}.")

    def _run_and_compare(self, run, site):
        from core.services.mrp.engine import run_mrp
        from core.models import MRPPlannedOrder, MRPDemand, MRPException
        run = MRPRun.objects.get(pk=run.pk)
        run_mrp(run)
        run.refresh_from_db()
        self.stdout.write(self.style.WARNING(f"\nActual engine output (run {run.run_number}, status {run.status}):"))

        pos = (MRPPlannedOrder.objects.filter(mrp_run=run)
               .select_related("product").order_by("product__sku", "id"))
        by_sku = {}
        for po in pos:
            by_sku.setdefault(po.product.sku, []).append(po)
        self.stdout.write("  Planned orders by product:")
        for sku in [FG_SKU] + [c[0] for c in COMPONENTS]:
            rows = by_sku.get(sku, [])
            if not rows:
                self.stdout.write(f"    {sku:12} (none)")
                continue
            for po in rows:
                self.stdout.write(
                    f"    {sku:12} {po.source_type:11} qty {po.quantity:>7} "
                    f"required {po.required_date} release {po.planned_release_date} "
                    f"receipt {po.planned_receipt_date}")
            if len(rows) > 1:
                total = sum((r.quantity for r in rows), ZERO)
                self.stdout.write(f"    {'':12} -> {len(rows)} orders, total {total}")

        dem = (MRPDemand.objects.filter(mrp_run=run, demand_type="WORK_ORDER_COMPONENT")
               .select_related("product").order_by("product__sku"))
        self.stdout.write(f"\n  WORK_ORDER_COMPONENT demand rows: {dem.count()}")
        for d in dem:
            self.stdout.write(f"    {d.product.sku:12} qty {d.quantity:>7} required {d.required_date}")

        excs = MRPException.objects.filter(mrp_run=run).order_by("exception_code")
        self.stdout.write(f"\n  Exceptions: {excs.count()}")
        seen = {}
        for e in excs:
            seen[e.exception_code] = seen.get(e.exception_code, 0) + 1
        for code, n in sorted(seen.items()):
            self.stdout.write(f"    {code} x{n}")


def InventoryMovement_exists(tenant, product, location, ref):
    from core.models import InventoryMovement
    return InventoryMovement.objects.filter(
        tenant=tenant, ref_type=SEED_REF, ref_id=ref, product=product, location=location).exists()
