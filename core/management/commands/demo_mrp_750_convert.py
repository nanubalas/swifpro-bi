"""One-click demo helper for the FG-750-DEMO MRP scenario.

    python manage.py demo_mrp_750_convert --tenant "MRP 750 Demo"
    python manage.py demo_mrp_750_convert --tenant "MRP 750 Demo" --buy-as po
    python manage.py demo_mrp_750_convert --tenant "MRP 750 Demo" --execute-work-order

Walks the converted-document flow after MRP has run:
  Sales order -> MAKE planned order -> component demand -> BUY planned orders
  -> convert (PR by default, or PO) + Work Order -> (optional) execute the WO.

Safe and idempotent: it only uses the existing conversion / work-order-execution
services (no MRP logic is touched, no quantities change). Re-running never
creates duplicate PR/PO/WO - the conversion service returns the existing linked
document. Inventory is posted ONLY when --execute-work-order is passed, and even
then materials are issued only where enough single-location stock exists and the
finished good is completed only if every material was fully issued.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from core.models import Tenant, MRPRun, MRPPlannedOrder, OrgMembership
from core.services.mrp import conversion

PREFERRED_RUN = "MRP-750-0-WRV7"

# (sku, source_type) -> expected planned quantity, for the confirmation check.
EXPECTED = [
    ("FG-750-DEMO", "MAKE", Decimal("750")),
    ("RM-A", "BUY", Decimal("900")),
    ("RM-B", "BUY", Decimal("250")),
    ("RM-C", "BUY", Decimal("1500")),
    ("RM-D", "BUY", Decimal("500")),
    ("RM-E", "BUY", Decimal("250")),
]


class Command(BaseCommand):
    help = "Convert (and optionally execute) the FG-750-DEMO MRP planned orders - safe and idempotent."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant name.")
        parser.add_argument("--buy-as", choices=["requisition", "pr", "po"], default="requisition",
                            help="Convert BUY planned orders to a requisition (default) or a draft PO.")
        parser.add_argument("--execute-work-order", action="store_true",
                            help="Firm/release the work order, issue available materials, "
                                 "book labour/overhead if rates exist, and complete only if fully issued.")

    def handle(self, *args, **opts):
        tenant = Tenant.objects.filter(name=opts["tenant"]).first()
        if tenant is None:
            raise CommandError(f"Tenant '{opts['tenant']}' not found. Run seed_mrp_750_scenario first.")
        run = (MRPRun.objects.filter(tenant=tenant, run_number=PREFERRED_RUN).first()
               or MRPRun.objects.filter(tenant=tenant, run_number__startswith="MRP-750").order_by("-id").first()
               or MRPRun.objects.filter(tenant=tenant).order_by("-id").first())
        if run is None:
            raise CommandError("No MRP run found for this tenant. Run the MRP run first.")
        user = self._demo_user(tenant)

        self.stdout.write(f"Tenant: {tenant.name}")
        self.stdout.write(f"MRP run: {run.run_number} (status {run.get_status_display()})")
        self._confirm_planned_orders(run)

        buy_target = conversion.PURCHASE_ORDER if opts["buy_as"] == "po" else conversion.REQUISITION
        rows = self._convert(run, user, buy_target)
        self._print_summary(rows)

        if opts["execute_work_order"]:
            self._execute(run, user)

    # ------------------------------------------------------------------ #
    def _demo_user(self, tenant):
        from django.contrib.auth.models import User
        m = (OrgMembership.objects.filter(tenant=tenant).select_related("user").order_by("id").first())
        if m:
            return m.user
        return User.objects.filter(is_superuser=True).order_by("id").first() or User.objects.order_by("id").first()

    def _confirm_planned_orders(self, run):
        self.stdout.write("\nConfirming planned orders:")
        ok = True
        for sku, st, exp in EXPECTED:
            total = sum((p.quantity for p in run.planned_orders.filter(product__sku=sku, source_type=st)),
                        Decimal("0"))
            mark = "OK" if total == exp else "MISMATCH"
            if total != exp:
                ok = False
            self.stdout.write(f"  {sku:12} {st:5} expected {exp:>7}  actual {total:>7}  [{mark}]")
        self.stdout.write(self.style.SUCCESS("  All planned orders correct.") if ok
                          else self.style.WARNING("  Some planned orders differ from the expected demo numbers."))

    def _convert(self, run, user, buy_target):
        rows = []
        order = ["FG-750-DEMO", "RM-A", "RM-B", "RM-C", "RM-D", "RM-E"]
        planned = list(run.planned_orders.select_related("product").all())
        planned.sort(key=lambda p: (order.index(p.product.sku) if p.product.sku in order else 99, p.id))
        for po in planned:
            target = conversion.WORK_ORDER if po.source_type == "MAKE" else buy_target
            try:
                doc, created, message = conversion.convert_planned_order(po, target, user)
            except conversion.ConversionError as e:
                rows.append((po, "-", "-", f"SKIPPED: {e}"))
                continue
            doc_type, doc_no = self._doc_label(doc)
            rows.append((po, doc_type, doc_no, "created" if created else "existing"))
        return rows

    def _doc_label(self, doc):
        for attr, label in (("req_number", "Requisition"), ("po_number", "PurchaseOrder"),
                            ("transfer_number", "TransferOrder"), ("work_order_number", "WorkOrder")):
            if hasattr(doc, attr):
                return label, getattr(doc, attr)
        return doc.__class__.__name__, str(getattr(doc, "id", "?"))

    def _print_summary(self, rows):
        self.stdout.write("\nConversion summary:")
        self.stdout.write(f"  {'Planned order':28} {'Src':5} {'Product':12} {'Qty':>8}  "
                          f"{'Document':13} {'Number':30} {'State'}")
        for po, doc_type, doc_no, state in rows:
            self.stdout.write(
                f"  {po.planned_order_number:28} {po.source_type:5} {po.product.sku:12} "
                f"{po.quantity:>8}  {doc_type:13} {str(doc_no):30} {state}")

    # ------------------------------------------------------------------ #
    def _execute(self, run, user):
        from core.services.mrp import work_order_execution as wox
        from core.services.mrp import work_order_labour as wol

        self.stdout.write(self.style.WARNING("\nExecuting work order (posts inventory for issued materials):"))
        fg = run.planned_orders.filter(source_type="MAKE").select_related("created_work_order").first()
        if fg is None or fg.created_work_order_id is None:
            self.stdout.write("  No work order to execute (convert the MAKE planned order first).")
            return
        wo = fg.created_work_order

        # 1. Firm + release (no stock impact), only as far as needed (idempotent).
        try:
            if wo.status == "PLANNED":
                wox.firm_work_order(wo, user)
                wox.release_work_order(wo, user)
            elif wo.status == "FIRM":
                wox.release_work_order(wo, user)
        except wox.WorkOrderError as e:
            self.stdout.write(f"  Could not release work order: {e}")
        wo.refresh_from_db()
        self.stdout.write(f"  Work order {wo.work_order_number}: status {wo.get_status_display()}")
        if wo.status not in ("RELEASED", "PARTIALLY_COMPLETED", "COMPLETED", "CLOSED"):
            self.stdout.write("  Work order is not released; stopping execution.")
            return

        # 2. Issue each material in full only where one nettable location can cover it.
        issued, skipped = 0, 0
        for wom in wo.materials.select_related("component").all():
            remaining = wom.remaining_quantity
            if remaining <= Decimal("0"):
                issued += 1  # already fully issued
                continue
            try:
                wox.issue_material(wom, remaining, user)
                issued += 1
                self.stdout.write(f"    issued {remaining} {wom.component.sku}")
            except wox.WorkOrderError as e:
                skipped += 1
                self.stdout.write(self.style.WARNING(
                    f"    {wom.component.sku}: not issued - {e}"))

        # 3. Labour / overhead - only if a work-centre rate exists.
        self._book_conversion(wo, user, wol)

        # 4. Complete the finished good only if every material was fully issued.
        wo.refresh_from_db()
        all_issued = wo.materials.exists() and all(
            m.remaining_quantity <= Decimal("0") for m in wo.materials.all())
        if not all_issued:
            self.stdout.write(self.style.WARNING(
                f"  Not all materials issued ({skipped} short); completion skipped (safe mode)."))
            return
        if wo.status in ("COMPLETED", "CLOSED"):
            self.stdout.write(f"  Work order already {wo.get_status_display()}.")
            return
        try:
            _movement, warning = wox.complete_work_order(wo, wo.quantity - (wo.quantity_completed or Decimal("0")), user)
            wo.refresh_from_db()
            self.stdout.write(self.style.SUCCESS(
                f"  Completed {wo.quantity_completed} {wo.product.sku}; status {wo.get_status_display()}."))
            if warning:
                self.stdout.write(self.style.WARNING(f"  Note: {warning}"))
        except wox.WorkOrderError as e:
            self.stdout.write(self.style.WARNING(f"  Completion skipped: {e}"))

    def _book_conversion(self, wo, user, wol):
        ops = list(wo.operations.select_related("work_centre").all()) if hasattr(wo, "operations") else []
        booked = False
        for op in ops:
            wc = op.work_centre
            lrate = (wc.labour_rate_per_hour if wc else None) or Decimal("0")
            orate = (wc.overhead_rate_per_hour if wc else None) or Decimal("0")
            if lrate <= Decimal("0") and orate <= Decimal("0"):
                continue
            lh = op.planned_labour_hours or op.planned_hours or Decimal("0")
            oh = op.planned_overhead_hours or op.planned_hours or Decimal("0")
            try:
                wol.book_operation_actuals(op, lh if lrate > 0 else 0, oh if orate > 0 else 0, user,
                                           note="Demo booking")
                booked = True
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"    labour/overhead booking skipped: {e}"))
        if not booked:
            self.stdout.write("    labour/overhead: no work-centre rates set; skipped.")
