from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase

from core.models import (
    Tenant, Location, Supplier, Product, PurchaseOrder, PurchaseOrderLine,
    Shipment, ShipmentLine, InventoryBalance, InventoryMovement, GoodsReceipt,
    UserProfile, Customer, CustomerInvoice, CustomerInvoiceLine, TaxCode,
)
from core.services.gl import post_customer_invoice


class ReceivingFlowTests(TestCase):
    """Locks in the fix for the previously-broken PO receiving flow."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme")
        self.loc = Location.objects.create(tenant=self.tenant, name="Main WH")
        self.supplier = Supplier.objects.create(tenant=self.tenant, name="Sup")
        self.product = Product.objects.create(tenant=self.tenant, sku="SKU-001", name="Widget")
        self.po = PurchaseOrder.objects.create(
            tenant=self.tenant, po_number="PO-1", supplier=self.supplier,
            status=PurchaseOrder.Status.SUBMITTED,
        )
        self.pol = PurchaseOrderLine.objects.create(
            po=self.po, product=self.product, ordered_qty=Decimal("10"), unit_cost=Decimal("2.50"),
        )
        self.shipment = Shipment.objects.create(
            tenant=self.tenant, po=self.po, from_supplier=self.supplier, destination=self.loc,
        )
        self.sl = ShipmentLine.objects.create(
            shipment=self.shipment, po_line=self.pol, expected_qty=Decimal("10"),
        )

        self.user = User.objects.create_user("wh", password="pw")
        wh_group, _ = Group.objects.get_or_create(name="Warehouse")
        self.user.groups.add(wh_group)
        UserProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.login(username="wh", password="pw")

    def test_receive_updates_inventory_and_ledger(self):
        resp = self.client.post(
            f"/po/{self.po.id}/receive/",
            {"grn_number": "GRN-TEST", f"recv_{self.sl.id}": "4"},
        )
        self.assertEqual(resp.status_code, 302)

        bal = InventoryBalance.objects.get(tenant=self.tenant, product=self.product, location=self.loc)
        self.assertEqual(bal.on_hand, Decimal("4.00"))

        mv = InventoryMovement.objects.get(tenant=self.tenant, product=self.product)
        self.assertEqual(mv.movement_type, InventoryMovement.MovementType.RECEIVE)
        self.assertEqual(mv.qty_delta, Decimal("4.00"))

        self.pol.refresh_from_db()
        self.assertEqual(self.pol.received_qty, Decimal("4.00"))
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.Status.PARTIALLY_RECEIVED)

    def test_over_receipt_is_rolled_back(self):
        self.client.post(f"/po/{self.po.id}/receive/", {f"recv_{self.sl.id}": "999"})
        # Whole transaction rolls back: no balance, no movement, no orphan GRN.
        self.assertFalse(InventoryBalance.objects.filter(tenant=self.tenant).exists())
        self.assertFalse(InventoryMovement.objects.filter(tenant=self.tenant).exists())
        self.assertFalse(GoodsReceipt.objects.filter(tenant=self.tenant).exists())

    def test_receive_requires_login(self):
        self.client.logout()
        resp = self.client.post(f"/po/{self.po.id}/receive/", {f"recv_{self.sl.id}": "1"})
        self.assertIn(resp.status_code, (302, 403))  # redirected to login or forbidden
        self.assertFalse(InventoryMovement.objects.filter(tenant=self.tenant).exists())


class TemplateRenderTests(TestCase):
    """Smoke-test that the redesigned templates render without errors."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme UI")
        self.user = User.objects.create_user("u", password="pw")
        self.user.groups.add(Group.objects.get_or_create(name="Read-only")[0])
        UserProfile.objects.create(user=self.user, tenant=self.tenant)

    def test_login_page_renders(self):
        resp = self.client.get("/login/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sign in to SKUNOW")

    def test_landing_page_renders(self):
        self.client.login(username="u", password="pw")
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "SKUNOW")
        self.assertContains(resp, "Work queue")

    def test_po_list_renders(self):
        self.user.groups.add(Group.objects.get_or_create(name="Admin")[0])
        self.client.login(username="u", password="pw")
        resp = self.client.get("/po/")
        self.assertEqual(resp.status_code, 200)


class PoAmendTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Amend Co")
        self.supplier = Supplier.objects.create(tenant=self.tenant, name="S")
        self.product = Product.objects.create(tenant=self.tenant, sku="SKU-1", name="P")
        self.po = PurchaseOrder.objects.create(
            tenant=self.tenant, po_number="PO-9", supplier=self.supplier,
            status=PurchaseOrder.Status.SUBMITTED, version=1,
        )
        PurchaseOrderLine.objects.create(po=self.po, product=self.product, ordered_qty=Decimal("5"), unit_cost=Decimal("1"))
        self.user = User.objects.create_user("p", password="pw")
        self.user.groups.add(Group.objects.get_or_create(name="Admin")[0])
        UserProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.login(username="p", password="pw")

    def test_amend_creates_versioned_po(self):
        resp = self.client.post(f"/po/{self.po.id}/amend/", {"reason": "price change"})
        self.assertEqual(resp.status_code, 302)
        new = PurchaseOrder.objects.get(tenant=self.tenant, version=2)
        self.assertEqual(new.po_number, "PO-9-v2")
        self.assertTrue(new.is_current)
        self.po.refresh_from_db()
        self.assertFalse(self.po.is_current)


class FormTenantScopeTests(TestCase):
    def test_supplier_dropdown_scoped_to_current_tenant(self):
        from core.current import set_current_tenant, clear_current_tenant
        from core.forms import PurchaseOrderForm

        t_a = Tenant.objects.create(name="Tenant A")
        t_b = Tenant.objects.create(name="Tenant B")
        Supplier.objects.create(tenant=t_a, name="A Supplier")
        Supplier.objects.create(tenant=t_b, name="B Supplier")

        set_current_tenant(t_a)
        try:
            names = list(PurchaseOrderForm().fields["supplier"].queryset.values_list("name", flat=True))
        finally:
            clear_current_tenant()
        self.assertEqual(names, ["A Supplier"])


class GLBalanceTests(TestCase):
    def test_supplier_invoice_journal_balances_with_vat(self):
        from core.models import (
            GoodsReceipt, GoodsReceiptLine, Location, SupplierInvoice, SupplierInvoiceLine,
        )
        from core.services.gl import post_supplier_invoice

        tenant = Tenant.objects.create(name="AP Co")
        supplier = Supplier.objects.create(tenant=tenant, name="Sup")
        product = Product.objects.create(tenant=tenant, sku="SKU-AP", name="P")
        loc = Location.objects.create(tenant=tenant, name="WH")
        po = PurchaseOrder.objects.create(tenant=tenant, po_number="PO-AP", supplier=supplier)
        grn = GoodsReceipt.objects.create(tenant=tenant, po=po, grn_number="GRN-AP", received_to=loc, status=GoodsReceipt.Status.POSTED)
        inv = SupplierInvoice.objects.create(tenant=tenant, supplier=supplier, po=po, receipt=grn, invoice_number="SINV-1")
        std = TaxCode.objects.get(tenant=tenant, code="STD")
        SupplierInvoiceLine.objects.create(invoice=inv, product=product, qty=Decimal("10"), unit_cost=Decimal("5.00"), tax_code=std)

        je = post_supplier_invoice(inv)
        self.assertEqual(je.total_debit, je.total_credit)
        self.assertEqual(je.total_credit, Decimal("60.00"))  # 50 net + 20% VAT input

    def test_customer_invoice_journal_balances(self):
        tenant = Tenant.objects.create(name="Acme2")  # signal bootstraps GL accounts + tax codes
        customer = Customer.objects.create(tenant=tenant, name="Cust")
        inv = CustomerInvoice.objects.create(tenant=tenant, customer=customer, invoice_number="INV-1")
        std = TaxCode.objects.get(tenant=tenant, code="STD")
        CustomerInvoiceLine.objects.create(
            invoice=inv, description="Item", qty=Decimal("2"), unit_price=Decimal("100.00"), tax_code=std,
        )

        je = post_customer_invoice(inv)

        self.assertEqual(je.total_debit, je.total_credit)
        self.assertEqual(je.total_debit, Decimal("240.00"))  # 200 net + 20% VAT
        inv.refresh_from_db()
        self.assertEqual(inv.status, "ISSUED")


class FinancialReportsTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Reports Co")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-R1")
        std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        CustomerInvoiceLine.objects.create(invoice=inv, description="Item", qty=Decimal("2"), unit_price=Decimal("100.00"), tax_code=std)
        post_customer_invoice(inv)  # DR AR 240 / CR Sales 200 / CR VAT 40

        self.user = User.objects.create_user("fin", password="pw")
        self.user.groups.add(Group.objects.get_or_create(name="Finance")[0])
        UserProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.login(username="fin", password="pw")

    def test_trial_balance_is_balanced(self):
        from core.services import reports
        tb = reports.trial_balance(self.tenant)
        self.assertTrue(tb["balanced"])
        self.assertEqual(tb["total_debit"], Decimal("240.00"))

    def test_pnl_net_profit(self):
        from core.services import reports
        pnl = reports.profit_and_loss(self.tenant)
        self.assertEqual(pnl["income_total"], Decimal("200.00"))
        self.assertEqual(pnl["expense_total"], Decimal("0.00"))
        self.assertEqual(pnl["net_profit"], Decimal("200.00"))

    def test_balance_sheet_balances(self):
        from core.services import reports
        bs = reports.balance_sheet(self.tenant)
        self.assertEqual(bs["asset_total"], Decimal("240.00"))          # AR
        self.assertEqual(bs["liability_total"], Decimal("40.00"))       # VAT output
        self.assertEqual(bs["retained_earnings"], Decimal("200.00"))    # net income
        self.assertTrue(bs["balanced"])

    def test_aged_receivables_lists_issued_invoice(self):
        from core.services import reports
        ar = reports.aged_receivables(self.tenant)
        self.assertEqual(ar["total"], Decimal("240.00"))
        self.assertEqual(len(ar["rows"]), 1)

    def test_report_pages_render(self):
        for path in ["/reports/", "/reports/trial-balance/", "/reports/profit-and-loss/",
                     "/reports/balance-sheet/", "/reports/aged-receivables/", "/reports/aged-payables/"]:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, f"{path} -> {resp.status_code}")
