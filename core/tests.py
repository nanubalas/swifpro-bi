from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase, Client

from core.models import (
    Tenant, Location, Supplier, Product, PurchaseOrder, PurchaseOrderLine,
    Shipment, ShipmentLine, InventoryBalance, InventoryMovement, GoodsReceipt,
    UserProfile, Customer, CustomerInvoice, CustomerInvoiceLine, TaxCode,
)
from core.services.gl import post_customer_invoice


class _CtxClient(Client):
    """Test client that auto-selects the user's default company + first
    accessible site on login, so the mandatory post-login context gate is
    satisfied. Mirrors the real auto-selection for single-context users; tests
    that exercise the gate/selection itself opt out via ``client_class = Client``.
    """

    def login(self, **credentials):
        ok = super().login(**credentials)
        if ok:
            self._select_default_context()
        return ok

    def _select_default_context(self):
        from core.access import (get_memberships, selectable_sites,
                                 SESSION_TENANT_KEY, SESSION_SITE_KEY)
        uid = self.session.get("_auth_user_id")
        if not uid:
            return
        u = User.objects.filter(pk=uid).first()
        if u is None:
            return
        memberships = get_memberships(u)
        if memberships:
            m = next((x for x in memberships if x.is_default), memberships[0])
            tenant = m.tenant
        else:
            prof = UserProfile.objects.filter(user=u).first()
            tenant = prof.tenant if prof else Tenant.objects.order_by("id").first()
        if tenant is None:
            return
        site = selectable_sites(u, tenant).first()
        s = self.session
        s[SESSION_TENANT_KEY] = tenant.id
        if site:
            s[SESSION_SITE_KEY] = site.id
        s.save()


# Apply suite-wide: every TestCase's self.client auto-selects a context on login.
TestCase.client_class = _CtxClient


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

        # Receipt capitalizes stock: DR Inventory / CR GRNI at 4 x 2.50 = 10.00
        from core.models import JournalEntry
        je = JournalEntry.objects.get(tenant=self.tenant, ref_type="GRN")
        self.assertEqual(je.total_debit, je.total_credit)
        self.assertEqual(je.total_debit, Decimal("10.00"))

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
        self.assertContains(resp, "SwifPro")  # wordmark
        self.assertContains(resp, "Business intelligence")

    def test_landing_page_renders(self):
        self.client.login(username="u", password="pw")
        resp = self.client.get("/", follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "SwifPro BI")
        self.assertContains(resp, "Dashboard")

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


class LocationProfileTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Loc Co")
        Location.objects.filter(tenant=self.tenant).delete()  # drop auto-seeded Main Location; this class manages its own
        self.user = User.objects.create_user("locu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="locu", password="pw")

    def test_storage_unit_type_available(self):
        from core.models import Location
        self.assertIn("STORAGE", dict(Location.Type.choices))
        self.assertEqual(dict(Location.Type.choices)["STORAGE"], "Storage room")
        # New inventory-location types from the spec are available too.
        for t in ["SHOP_FLOOR", "BACK_ROOM", "COLD_STORAGE", "DAMAGED"]:
            self.assertIn(t, dict(Location.Type.choices))

    def test_create_with_all_fields(self):
        from core.models import Location
        resp = self.client.post("/locations/new/", {
            "name": "Camden Shop", "type": "STORE", "address": "1 High St\nLondon",
            "contact_person": "Sam", "phone": "+44 20 7946 0000", "email": "shop@x.example",
            "opening_hours": "Mon-Fri 9-5", "is_active": "on", "holds_stock": "on",
        })
        self.assertEqual(resp.status_code, 302)
        loc = Location.objects.get(tenant=self.tenant, name="Camden Shop")
        self.assertEqual(loc.type, "STORE")
        self.assertEqual(loc.contact_person, "Sam")
        self.assertEqual(loc.email, "shop@x.example")
        self.assertEqual(loc.opening_hours, "Mon-Fri 9-5")
        self.assertTrue(loc.is_active)
        self.assertTrue(loc.holds_stock)

    def test_inactive_or_nonstock_location_excluded_from_stock_form(self):
        from core.current import set_current_tenant, clear_current_tenant
        from core.forms import StockAdjustmentForm
        from core.models import Location
        Location.objects.create(tenant=self.tenant, name="Live WH", type="WAREHOUSE", is_active=True, holds_stock=True)
        Location.objects.create(tenant=self.tenant, name="Closed WH", type="WAREHOUSE", is_active=False, holds_stock=True)
        Location.objects.create(tenant=self.tenant, name="Head Office", type="OFFICE", is_active=True, holds_stock=False)
        set_current_tenant(self.tenant)
        try:
            names = set(StockAdjustmentForm().fields["location"].queryset.values_list("name", flat=True))
        finally:
            clear_current_tenant()
        self.assertEqual(names, {"Live WH"})


class CompanyGroupTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, CompanyGroup
        self.grp = CompanyGroup.objects.create(name="Acme Holdings")
        self.t1 = Tenant.objects.create(name="Acme UK", group=self.grp)
        self.t2 = Tenant.objects.create(name="Acme EU", group=self.grp)
        self.t3 = Tenant.objects.create(name="Other Co")  # no group
        self.user = User.objects.create_user("grpu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.t1, role="ADMIN", is_default=True)
        OrgMembership.objects.create(user=self.user, tenant=self.t2, role="ADMIN")

    def test_group_companies_limited_to_membership(self):
        from core.access import group_companies
        from core.models import Tenant
        Tenant.objects.create(name="Acme US", group=self.grp)  # user not a member -> excluded
        names = {t.name for t in group_companies(self.user, self.t1)}
        self.assertEqual(names, {"Acme UK", "Acme EU"})

    def test_no_group_returns_self(self):
        from core.access import group_companies
        self.assertEqual([t.name for t in group_companies(self.user, self.t3)], ["Other Co"])

    def test_create_group_via_view(self):
        self.client.login(username="grpu", password="pw")
        resp = self.client.post("/settings/group/", {"op": "create", "name": "NewGroup"})
        self.assertEqual(resp.status_code, 302)
        self.t1.refresh_from_db()
        self.assertEqual(self.t1.group.name, "NewGroup")

    def test_group_page_renders(self):
        self.client.login(username="grpu", password="pw")
        self.assertEqual(self.client.get("/settings/group/").status_code, 200)

    def test_consolidated_sums_across_companies(self):
        from core.services import reports
        from core.services.gl import post_customer_invoice
        from core.models import Customer, CustomerInvoice, CustomerInvoiceLine, TaxCode
        for t, net in [(self.t1, "200"), (self.t2, "300")]:
            std = TaxCode.objects.get(tenant=t, code="STD")
            c = Customer.objects.create(tenant=t, name=f"C-{t.id}")
            inv = CustomerInvoice.objects.create(tenant=t, customer=c, invoice_number=f"INV-{t.id}")
            CustomerInvoiceLine.objects.create(invoice=inv, description="X", qty=Decimal("1"),
                                               unit_price=Decimal(net), tax_code=std)
            post_customer_invoice(inv)
        data = reports.consolidated([self.t1, self.t2])
        self.assertEqual(len(data["rows"]), 2)
        self.assertEqual(data["totals"]["revenue"], Decimal("500.00"))

    def test_consolidated_page_renders(self):
        self.client.login(username="grpu", password="pw")
        resp = self.client.get("/reports/consolidated/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["company_count"], 2)


class InterCompanyTradingTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, CompanyGroup
        self.grp = CompanyGroup.objects.create(name="Holdings")
        self.a = Tenant.objects.create(name="Company A", group=self.grp)
        self.b = Tenant.objects.create(name="Company B", group=self.grp)
        self.user = User.objects.create_user("icu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.a, role="ADMIN", is_default=True)
        OrgMembership.objects.create(user=self.user, tenant=self.b, role="ADMIN")

    def _bal(self, tenant, code):
        from core.models import GLAccount, JournalLine
        from django.db.models import Sum
        acc = GLAccount.objects.get(tenant=tenant, code=code)
        agg = JournalLine.objects.filter(account=acc).aggregate(d=Sum("debit"), c=Sum("credit"))
        return (agg["d"] or Decimal("0")) - (agg["c"] or Decimal("0"))

    def test_sale_posts_ar_in_seller_and_ap_in_buyer(self):
        from core.services.intercompany import create_intercompany_sale
        ict = create_intercompany_sale(self.a, self.b, Decimal("500"), "Mgmt fee", user=self.user)
        self.assertEqual(self._bal(self.a, "1100"), Decimal("500.00"))   # AR in A
        self.assertEqual(self._bal(self.a, "4000"), Decimal("-500.00"))  # sales in A
        self.assertTrue(ict.customer_invoice.is_intercompany)
        self.assertEqual(self._bal(self.b, "6000"), Decimal("500.00"))   # expense in B
        self.assertEqual(self._bal(self.b, "2000"), Decimal("-500.00"))  # AP in B
        self.assertTrue(ict.expense.is_intercompany)

    def test_consolidation_eliminates_intragroup(self):
        from core.services.intercompany import create_intercompany_sale
        from core.services import reports
        create_intercompany_sale(self.a, self.b, Decimal("500"), "fee", user=self.user)
        data = reports.consolidated([self.a, self.b])
        self.assertEqual(data["totals"]["eliminations"], Decimal("500.00"))
        self.assertEqual(data["totals"]["revenue"] - data["totals"]["net_revenue"], Decimal("500.00"))

    def test_create_via_view_and_validation(self):
        from core.models import InterCompanyTransaction
        self.client.login(username="icu", password="pw")
        resp = self.client.post("/intercompany/", {"to_tenant": self.b.id, "amount": "250", "description": "svc"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(InterCompanyTransaction.objects.filter(from_tenant=self.a, to_tenant=self.b).count(), 1)
        self.client.post("/intercompany/", {"to_tenant": self.b.id, "amount": "0"})  # rejected
        self.assertEqual(InterCompanyTransaction.objects.count(), 1)


class SiteTierTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Site Co")
        self.user = User.objects.create_user("siteu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="siteu", password="pw")

    def test_create_site_and_assign_location(self):
        from core.models import Site, Location
        resp = self.client.post("/sites/new/", {"name": "Manchester Plant", "code": "MCR", "is_active": "on"})
        self.assertEqual(resp.status_code, 302)
        site = Site.objects.get(tenant=self.tenant, name="Manchester Plant")
        resp = self.client.post("/locations/new/", {
            "name": "MCR Warehouse", "site": site.id, "type": "WAREHOUSE",
            "is_active": "on", "holds_stock": "on",
        })
        self.assertEqual(resp.status_code, 302)
        loc = Location.objects.get(tenant=self.tenant, name="MCR Warehouse")
        self.assertEqual(loc.site_id, site.id)

    def test_site_list_renders_with_locations(self):
        from core.models import Site, Location
        site = Site.objects.create(tenant=self.tenant, name="HQ")
        Location.objects.create(tenant=self.tenant, name="HQ WH", type="WAREHOUSE", site=site)
        resp = self.client.get("/sites/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "HQ WH")

    def test_site_scoped_to_tenant_in_location_form(self):
        from core.current import set_current_tenant, clear_current_tenant
        from core.forms import LocationForm
        from core.models import Site, Tenant as T
        Site.objects.create(tenant=self.tenant, name="Mine")
        other = T.objects.create(name="Other")
        Site.objects.create(tenant=other, name="Theirs")
        set_current_tenant(self.tenant)
        try:
            names = set(LocationForm().fields["site"].queryset.values_list("name", flat=True))
        finally:
            clear_current_tenant()
        self.assertEqual(names, {"Main Site", "Mine"})  # tenant's sites only (auto + own), not "Theirs"


class BinTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, Location
        self.tenant = Tenant.objects.create(name="Bin Co")
        self.wh = Location.objects.create(tenant=self.tenant, name="WH", type="WAREHOUSE", is_active=True, holds_stock=True)
        self.user = User.objects.create_user("binu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="binu", password="pw")

    def test_create_bin(self):
        from core.models import Bin
        resp = self.client.post("/bins/new/", {"location": self.wh.id, "code": "A-01", "is_active": "on"})
        self.assertEqual(resp.status_code, 302)
        b = Bin.objects.get(tenant=self.tenant)
        self.assertEqual(b.code, "A-01")
        self.assertEqual(b.location_id, self.wh.id)

    def test_adjustment_records_bin_on_movement(self):
        from core.models import Product, Bin, StockAdjustment, InventoryMovement
        p = Product.objects.create(tenant=self.tenant, sku="B1", name="P")
        b = Bin.objects.create(tenant=self.tenant, location=self.wh, code="A-02")
        resp = self.client.post("/inventory/adjustments/new/", {
            "product": p.id, "location": self.wh.id, "bin": b.id,
            "reason": "ADJUSTMENT", "qty_delta": "5",
        })
        self.assertEqual(resp.status_code, 302)
        adj = StockAdjustment.objects.get(tenant=self.tenant)
        self.assertEqual(adj.bin_id, b.id)
        mv = InventoryMovement.objects.get(tenant=self.tenant, product=p)
        self.assertEqual(mv.bin_id, b.id)

    def test_bin_must_match_location(self):
        from core.models import Product, Bin, Location, StockAdjustment
        p = Product.objects.create(tenant=self.tenant, sku="B2", name="P")
        other = Location.objects.create(tenant=self.tenant, name="WH2", type="WAREHOUSE", is_active=True, holds_stock=True)
        b = Bin.objects.create(tenant=self.tenant, location=other, code="X-01")
        resp = self.client.post("/inventory/adjustments/new/", {
            "product": p.id, "location": self.wh.id, "bin": b.id,
            "reason": "ADJUSTMENT", "qty_delta": "5",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Bin must belong to the chosen location")
        self.assertEqual(StockAdjustment.objects.filter(tenant=self.tenant).count(), 0)


class LocationAccessTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, Location, InventoryBalance, Product
        self.tenant = Tenant.objects.create(name="Acc Co")
        self.wh1 = Location.objects.create(tenant=self.tenant, name="WH1", type="WAREHOUSE")
        self.wh2 = Location.objects.create(tenant=self.tenant, name="WH2", type="WAREHOUSE")
        self.p = Product.objects.create(tenant=self.tenant, sku="A1", name="P")
        InventoryBalance.objects.create(tenant=self.tenant, product=self.p, location=self.wh1, on_hand=Decimal("10"))
        InventoryBalance.objects.create(tenant=self.tenant, product=self.p, location=self.wh2, on_hand=Decimal("5"))
        self.admin = User.objects.create_user("accadmin", password="pw")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)
        self.wh = User.objects.create_user("accwh", password="pw")
        OrgMembership.objects.create(user=self.wh, tenant=self.tenant, role="WAREHOUSE", is_default=True)

    def test_unrestricted_by_default(self):
        from core.access import accessible_location_ids
        self.assertIsNone(accessible_location_ids(self.wh, self.tenant))  # no grants -> all

    def test_admin_always_unrestricted(self):
        from core.access import accessible_location_ids
        from core.models import UserLocationAccess
        UserLocationAccess.objects.create(tenant=self.tenant, user=self.admin, location=self.wh1)
        self.assertIsNone(accessible_location_ids(self.admin, self.tenant))  # admin -> all despite a grant

    def test_grant_restricts_inventory_and_reports(self):
        from core.models import UserLocationAccess
        from core.services import reports
        from core.access import accessible_location_ids
        UserLocationAccess.objects.create(tenant=self.tenant, user=self.wh, location=self.wh1)
        self.client.login(username="accwh", password="pw")
        resp = self.client.get("/inventory/")
        self.assertEqual(resp.status_code, 200)
        locs = {b.location.name for b in resp.context["balances"]}
        self.assertEqual(locs, {"WH1"})
        allowed = accessible_location_ids(self.wh, self.tenant)
        data = reports.stock_valuation(self.tenant, location_ids=allowed)
        qty = sum(r["qty"] for r in data["rows"])
        self.assertEqual(qty, Decimal("10"))

    def test_access_matrix_admin_only_and_saves(self):
        from core.models import UserLocationAccess
        self.client.login(username="accwh", password="pw")
        self.assertEqual(self.client.get("/locations/access/").status_code, 403)
        self.client.login(username="accadmin", password="pw")
        self.assertEqual(self.client.get("/locations/access/").status_code, 200)
        resp = self.client.post("/locations/access/", {f"grant_{self.wh.id}_{self.wh1.id}": "on"})
        self.assertEqual(resp.status_code, 302)
        grants = set(UserLocationAccess.objects.filter(tenant=self.tenant, user=self.wh).values_list("location_id", flat=True))
        self.assertEqual(grants, {self.wh1.id})


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


class RoleDashboardTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Role Co")
        self.user = User.objects.create_user("salesuser", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="SALES", is_default=True)

    def test_login_redirects_to_role_dashboard(self):
        self.client.login(username="salesuser", password="pw")
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/dashboard/sales")

    def test_role_dashboard_renders(self):
        self.client.login(username="salesuser", password="pw")
        resp = self.client.get("/dashboard/sales")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sales Dashboard")

    def test_cross_role_dashboard_is_forbidden_and_audited(self):
        from core.models import AuditLog
        self.client.login(username="salesuser", password="pw")
        resp = self.client.get("/dashboard/finance")
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "Access denied", status_code=403)
        self.assertTrue(AuditLog.objects.filter(action="ACCESS_DENIED").exists())

    def test_admin_can_view_any_dashboard(self):
        from core.models import OrgMembership
        admin = User.objects.create_user("owneruser", password="pw")
        OrgMembership.objects.create(user=admin, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="owneruser", password="pw")
        self.assertEqual(self.client.get("/dashboard/warehouse").status_code, 200)
        self.assertEqual(self.client.get("/dashboard/finance").status_code, 200)

    def test_multi_org_redirects_to_picker(self):
        from core.models import OrgMembership
        t2 = Tenant.objects.create(name="Org Two")
        OrgMembership.objects.create(user=self.user, tenant=t2, role="ACCOUNTANT")
        self.client.login(username="salesuser", password="pw")
        # Simulate a fresh login with no context chosen yet (the auto-context
        # client pre-selects one; clear it to exercise the multi-company gate).
        s = self.client.session
        s.pop("active_tenant_id", None)
        s.pop("active_location_id", None)
        s.save()
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/select-org/", resp.url)

    def test_login_is_audited(self):
        from core.models import AuditLog
        self.client.post("/login/", {"username": "salesuser", "password": "pw"})
        self.assertTrue(AuditLog.objects.filter(action="LOGIN", username="salesuser").exists())

    def test_logout_via_post(self):
        self.client.login(username="salesuser", password="pw")
        resp = self.client.post("/logout/")
        self.assertEqual(resp.status_code, 302)
        # session cleared -> protected page now redirects to login
        self.assertEqual(self.client.get("/dashboard/sales").status_code, 302)

    def test_role_landing_override(self):
        self.tenant.role_landing = {"SALES": "reports_index"}
        self.tenant.save()
        self.client.login(username="salesuser", password="pw")
        resp = self.client.get("/")
        self.assertEqual(resp.url, "/reports/")


class DashboardKpiTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="KPI Co")
        self.admin = User.objects.create_user("kpiadmin", password="pw")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)

    def test_each_role_dashboard_has_kpis(self):
        self.client.login(username="kpiadmin", password="pw")
        for path in ["admin", "finance", "sales", "warehouse", "purchasing", "accountant", "read-only"]:
            resp = self.client.get(f"/dashboard/{path}")
            self.assertEqual(resp.status_code, 200, path)
            self.assertTrue(len(resp.context["kpis"]) >= 3, f"{path} has too few KPIs")

    def test_kpis_reflect_data(self):
        from core.models import Product, InventoryBalance, Location, PurchaseOrder, PurchaseOrderLine, Supplier
        sup = Supplier.objects.create(tenant=self.tenant, name="S")
        loc = Location.objects.create(tenant=self.tenant, name="WH", type=Location.Type.WAREHOUSE)
        p = Product.objects.create(tenant=self.tenant, sku="K1", name="P", reorder_level=Decimal("10"), is_active=True)
        InventoryBalance.objects.create(tenant=self.tenant, product=p, location=loc, on_hand=Decimal("2"))
        po = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-K", supplier=sup,
                                          status=PurchaseOrder.Status.SENT)
        PurchaseOrderLine.objects.create(po=po, product=p, ordered_qty=Decimal("5"), unit_cost=Decimal("1"))
        self.client.login(username="kpiadmin", password="pw")
        kpis = {k["label"]: k["value"] for k in self.client.get("/dashboard/purchasing").context["kpis"]}
        self.assertEqual(kpis["Open purchase orders"], 1)
        self.assertEqual(kpis["Low-stock items"], 1)


class CompanyProfileTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Profile Co")
        self.user = User.objects.create_user("padmin", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="padmin", password="pw")

    def _base_post(self, **overrides):
        data = {
            "name": "Profile Co", "legal_name": "Profile Co Ltd", "trading_name": "Profile",
            "business_type": "LTD", "company_number": "12345678", "utr_number": "1234567890",
            "vat_number": "GB123456789",
            "address_line1": "1 High St", "address_city": "Manchester", "address_postcode": "M1 2AB",
            "address_country": "United Kingdom",
            "billing_same_as_business": "on", "billing_country": "United Kingdom",
            "email": "ops@profile.test", "phone": "+44 20 7946 0000", "website": "https://profile.test",
            "currency_code": "GBP", "country": "United Kingdom", "timezone": "Europe/London",
            "financial_year_start_month": "4", "default_payment_terms_days": "30",
            "po_approval_threshold": "0",
        }
        data.update(overrides)
        return data

    def test_settings_page_renders(self):
        resp = self.client.get("/settings/tenant/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Company Profile")

    def test_save_full_profile(self):
        resp = self.client.post("/settings/tenant/", self._base_post())
        self.assertEqual(resp.status_code, 302)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.business_type, "LTD")
        self.assertEqual(self.tenant.legal_name, "Profile Co Ltd")
        self.assertEqual(self.tenant.financial_year_start_month, 4)
        self.assertEqual(self.tenant.default_payment_terms_days, 30)

    def test_invalid_company_number_rejected(self):
        resp = self.client.post("/settings/tenant/", self._base_post(company_number="ABC"))
        self.assertEqual(resp.status_code, 200)  # re-rendered with errors
        self.assertContains(resp, "valid UK company number")

    def test_invalid_vat_number_rejected(self):
        resp = self.client.post("/settings/tenant/", self._base_post(vat_number="12"))
        self.assertContains(resp, "valid UK VAT number")

    def test_vat_required_when_registered(self):
        resp = self.client.post("/settings/tenant/", self._base_post(vat_registered="on", vat_number=""))
        self.assertContains(resp, "VAT number is required")

    def test_required_fields_enforced(self):
        # Blank legal name / address must be rejected (re-rendered, not saved).
        resp = self.client.post("/settings/tenant/", self._base_post(legal_name="", address_line1=""))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["form"].is_valid())


class UserManagementTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Members Co")
        self.admin = User.objects.create_user("mgadmin", password="pw")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)
        self.bob = User.objects.create_user("bob", password="pw")
        self.bob_m = OrgMembership.objects.create(user=self.bob, tenant=self.tenant, role="SALES", is_default=True)
        self.client.login(username="mgadmin", password="pw")

    def test_members_list_renders(self):
        resp = self.client.get("/users/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "bob")

    def test_change_role_audited(self):
        from core.models import OrgMembership, AuditLog
        resp = self.client.post(f"/users/{self.bob_m.id}/role/", {"role": "WAREHOUSE"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(OrgMembership.objects.get(id=self.bob_m.id).role, "WAREHOUSE")
        self.assertTrue(AuditLog.objects.filter(action="ROLE_CHANGED").exists())

    def test_deactivate_and_remove(self):
        from core.models import OrgMembership, AuditLog
        self.client.post(f"/users/{self.bob_m.id}/active/")
        self.bob.refresh_from_db()
        self.assertFalse(self.bob.is_active)
        self.assertTrue(AuditLog.objects.filter(action="USER_DEACTIVATED").exists())
        self.client.post(f"/users/{self.bob_m.id}/remove/")
        self.assertFalse(OrgMembership.objects.filter(id=self.bob_m.id).exists())
        self.assertTrue(AuditLog.objects.filter(action="USER_REMOVED").exists())

    def test_cannot_remove_last_admin(self):
        from core.models import OrgMembership
        admin_m = OrgMembership.objects.get(user=self.admin, tenant=self.tenant)
        resp = self.client.post(f"/users/{admin_m.id}/remove/")
        self.assertTrue(OrgMembership.objects.filter(id=admin_m.id).exists())  # blocked

    def test_non_admin_blocked(self):
        c = Client(); c.login(username="bob", password="pw")
        self.assertEqual(c.get("/users/").status_code, 403)


class AuditTrailPhase3Tests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, Product
        self.tenant = Tenant.objects.create(name="Audit Co")
        self.admin = User.objects.create_user("auadmin", password="pw")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)
        self.sales = User.objects.create_user("ausales", password="pw")
        OrgMembership.objects.create(user=self.sales, tenant=self.tenant, role="SALES", is_default=True)
        self.product = Product.objects.create(tenant=self.tenant, sku="SKU-DEL", name="Doomed")

    def test_record_delete_audited(self):
        from core.models import AuditLog
        self.client.login(username="auadmin", password="pw")
        resp = self.client.post(f"/products/{self.product.id}/delete/")
        self.assertEqual(resp.status_code, 302)
        log = AuditLog.objects.filter(action="RECORD_DELETED").first()
        self.assertIsNotNone(log)
        self.assertIn("SKU-DEL", log.detail)

    def test_data_export_csv_and_audit(self):
        from core.models import AuditLog
        self.client.login(username="auadmin", password="pw")
        resp = self.client.get("/export/products.csv")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv")
        self.assertIn("SKU-DEL", resp.content.decode())
        self.assertTrue(AuditLog.objects.filter(action="DATA_EXPORTED").exists())

    def test_data_export_blocked_without_permission(self):
        self.client.login(username="ausales", password="pw")  # SALES lacks export_data
        self.assertEqual(self.client.get("/export/products.csv").status_code, 403)

    def test_audit_log_export_admin_only(self):
        self.client.login(username="ausales", password="pw")
        self.assertEqual(self.client.get("/audit/export.csv").status_code, 403)
        self.client.login(username="auadmin", password="pw")
        resp = self.client.get("/audit/export.csv")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("timestamp", resp.content.decode())

    def test_password_change_audited(self):
        from core.models import AuditLog
        self.client.login(username="auadmin", password="pw")
        resp = self.client.post("/account/password/", {
            "old_password": "pw",
            "new_password1": "Str0ng-Pass-99",
            "new_password2": "Str0ng-Pass-99",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(AuditLog.objects.filter(action="PASSWORD_CHANGED").exists())
        self.assertTrue(self.client.login(username="auadmin", password="Str0ng-Pass-99"))


class AuditSoftDeleteTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, GLAccount
        self.tenant = Tenant.objects.create(name="SoftDel Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.admin = User.objects.create_user("sdadmin", password="pw")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="sdadmin", password="pw")

    def _issued_invoice(self, number, net="200"):
        from core.models import CustomerInvoice, CustomerInvoiceLine, Customer
        from core.services.gl import post_customer_invoice
        cust = Customer.objects.create(tenant=self.tenant, name=f"C-{number}")
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=cust, invoice_number=number)
        CustomerInvoiceLine.objects.create(invoice=inv, description="X", qty=Decimal("1"),
                                           unit_price=Decimal(net), tax_code=self.std)
        post_customer_invoice(inv)
        return inv

    def test_product_cost_change_audited(self):
        from core.models import Product, AuditLog
        p = Product.objects.create(tenant=self.tenant, sku="PC1", name="W", standard_cost=Decimal("5.00"))
        resp = self.client.post(f"/products/{p.id}/edit/", {
            "sku": "PC1", "name": "W", "product_type": "STOCK", "uom": "each",
            "cost_method": "AVERAGE", "standard_cost": "8.50", "sales_price": "0", "reorder_level": "0",
        })
        self.assertEqual(resp.status_code, 302)
        log = AuditLog.objects.get(action="PRODUCT_COST_CHANGED")
        self.assertEqual(log.entity_id, "PC1")
        self.assertEqual(log.old_value, "5.00")
        self.assertEqual(log.new_value, "8.50")

    def test_draft_invoice_soft_delete(self):
        from core.models import CustomerInvoice, Customer, AuditLog
        cust = Customer.objects.create(tenant=self.tenant, name="Draft Co")
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=cust, invoice_number="INV-D1")
        resp = self.client.post(f"/ar/invoices/{inv.id}/delete/")
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(CustomerInvoice.objects.filter(id=inv.id).exists())          # hidden
        self.assertTrue(CustomerInvoice.all_objects.filter(id=inv.id, is_deleted=True).exists())  # kept
        self.assertTrue(AuditLog.objects.filter(action="INVOICE_DELETED").exists())

    def test_posted_invoice_cannot_be_deleted(self):
        from core.models import CustomerInvoice
        inv = self._issued_invoice("INV-P1")
        self.client.post(f"/ar/invoices/{inv.id}/delete/")
        self.assertTrue(CustomerInvoice.objects.filter(id=inv.id).exists())  # still there, not deleted

    def test_payment_soft_delete_reverses_gl_and_reopens_invoice(self):
        from core.models import CustomerInvoice, Payment, PaymentAllocation, JournalEntry, AuditLog
        from core.services.gl import post_payment
        inv = self._issued_invoice("INV-PAY")  # total 240
        pay = Payment.objects.create(tenant=self.tenant, customer=inv.customer,
                                     direction=Payment.Direction.RECEIPT, amount=Decimal("240"),
                                     payment_date="2026-06-01")
        PaymentAllocation.objects.create(payment=pay, customer_invoice=inv, amount=Decimal("240"))
        post_payment(pay)
        inv.refresh_from_db()
        self.assertEqual(inv.status, CustomerInvoice.Status.PAID)

        resp = self.client.post(f"/payments/{pay.id}/delete/")
        self.assertEqual(resp.status_code, 302)
        # payment hidden but retained
        self.assertFalse(Payment.objects.filter(id=pay.id).exists())
        self.assertTrue(Payment.all_objects.filter(id=pay.id, is_deleted=True).exists())
        # reversing JE posted + invoice re-opened with full outstanding
        self.assertTrue(JournalEntry.objects.filter(tenant=self.tenant, ref_type="PAYMENT_REVERSAL", ref_id=str(pay.id)).exists())
        inv.refresh_from_db()
        self.assertEqual(inv.status, CustomerInvoice.Status.SENT)
        self.assertEqual(inv.outstanding, Decimal("240.00"))
        self.assertTrue(AuditLog.objects.filter(action="PAYMENT_DELETED").exists())


class AuditLogStructuredTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="AuditS Co")
        self.admin = User.objects.create_user("asadmin", password="pw")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)
        self.sales = User.objects.create_user("assales", password="pw")
        OrgMembership.objects.create(user=self.sales, tenant=self.tenant, role="SALES", is_default=True)

    def test_log_audit_captures_structured_fields_and_user_agent(self):
        from core.audit import log_audit
        from core.models import AuditLog

        class FakeReq:
            path = "/x/"
            META = {"REMOTE_ADDR": "10.0.0.5", "HTTP_USER_AGENT": "Mozilla/5.0 TestBrowser"}
        log_audit(action="UPDATE", request=FakeReq(), user=self.admin, tenant=self.tenant,
                  entity_type="Product", entity_id="42", old_value="5.00", new_value="7.50",
                  detail="cost change")
        log = AuditLog.objects.get(action="UPDATE")
        self.assertEqual(log.entity_type, "Product")
        self.assertEqual(log.entity_id, "42")
        self.assertEqual(log.old_value, "5.00")
        self.assertEqual(log.new_value, "7.50")
        self.assertEqual(log.ip, "10.0.0.5")
        self.assertEqual(log.user_agent, "Mozilla/5.0 TestBrowser")
        self.assertEqual(log.change_summary, "5.00 → 7.50")

    def test_audit_log_is_immutable(self):
        from core.models import AuditLog
        log = AuditLog.objects.create(tenant=self.tenant, action="LOGIN", username="x")
        log.action = "TAMPERED"
        with self.assertRaises(ValueError):
            log.save()
        log.refresh_from_db()
        self.assertEqual(log.action, "LOGIN")

    def test_viewer_filters_by_action_and_is_admin_only(self):
        from core.audit import log_audit
        log_audit(action="LOGIN", user=self.admin, tenant=self.tenant)
        log_audit(action="DATA_EXPORTED", user=self.admin, tenant=self.tenant)
        # Non-admin cannot view.
        self.client.login(username="assales", password="pw")
        self.assertEqual(self.client.get("/audit/").status_code, 403)
        # Admin can, and the action filter narrows results.
        self.client.login(username="asadmin", password="pw")
        resp = self.client.get("/audit/?action=DATA_EXPORTED")
        self.assertEqual(resp.status_code, 200)
        actions = {l.action for l in resp.context["logs"]}
        self.assertEqual(actions, {"DATA_EXPORTED"})


class UserPermissionOverrideTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, UserPermissionOverride
        self.tenant = Tenant.objects.create(name="Grants Co")
        self.admin = User.objects.create_user("gradmin", password="pw")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)
        self.wh = User.objects.create_user("grwh", password="pw")
        self.wh_m = OrgMembership.objects.create(user=self.wh, tenant=self.tenant, role="WAREHOUSE", is_default=True)

    def test_effective_permissions_grant_and_revoke(self):
        from core import permissions as P
        # baseline: WAREHOUSE lacks view_finance_reports, has manage_inventory
        base = P.role_permissions("WAREHOUSE")
        self.assertNotIn(P.VIEW_FINANCE_REPORTS, base)
        eff = P.effective_permissions("WAREHOUSE", {P.VIEW_FINANCE_REPORTS: P.GRANT, P.MANAGE_INVENTORY: P.REVOKE})
        self.assertIn(P.VIEW_FINANCE_REPORTS, eff)
        self.assertNotIn(P.MANAGE_INVENTORY, eff)

    def test_admin_always_full_regardless_of_overrides(self):
        from core import permissions as P
        eff = P.effective_permissions("ADMIN", {P.MANAGE_USERS: P.REVOKE})
        self.assertEqual(eff, set(P.ALL_PERMISSIONS))

    def test_grant_enables_gated_view(self):
        from core.models import UserPermissionOverride
        from core import permissions as P
        # WAREHOUSE lacks export_data -> export blocked
        self.client.login(username="grwh", password="pw")
        self.assertEqual(self.client.get("/export/products.csv").status_code, 403)
        # grant export_data -> now allowed
        UserPermissionOverride.objects.create(tenant=self.tenant, user=self.wh,
                                              permission=P.EXPORT_DATA, effect=UserPermissionOverride.GRANT)
        self.assertEqual(self.client.get("/export/products.csv").status_code, 200)

    def test_editor_saves_overrides_and_audits(self):
        from core.models import UserPermissionOverride, AuditLog
        from core import permissions as P
        self.client.login(username="gradmin", password="pw")
        # WAREHOUSE baseline perms that should stay ticked, plus grant export_data
        data = {f"perm_{c}": "on" for c in P.role_permissions("WAREHOUSE")}
        data[f"perm_{P.EXPORT_DATA}"] = "on"
        resp = self.client.post(f"/users/{self.wh_m.id}/permissions/", data)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(UserPermissionOverride.objects.filter(
            tenant=self.tenant, user=self.wh, permission=P.EXPORT_DATA,
            effect=UserPermissionOverride.GRANT).exists())
        self.assertTrue(AuditLog.objects.filter(action="PERMISSION_CHANGED").exists())

    def test_role_change_clears_overrides_by_default(self):
        from core.models import UserPermissionOverride
        from core import permissions as P
        UserPermissionOverride.objects.create(tenant=self.tenant, user=self.wh,
                                              permission=P.EXPORT_DATA, effect=UserPermissionOverride.GRANT)
        self.client.login(username="gradmin", password="pw")
        self.client.post(f"/users/{self.wh_m.id}/role/", {"role": "SALES"})
        self.assertFalse(UserPermissionOverride.objects.filter(tenant=self.tenant, user=self.wh).exists())

    def test_role_change_keeps_overrides_when_policy_on(self):
        from core.models import UserPermissionOverride
        from core import permissions as P
        self.tenant.keep_permissions_on_role_change = True
        self.tenant.save()
        # A meaningful grant (SALES lacks export_data) and a redundant one
        # (SALES already has manage_customers, so granting it is redundant).
        UserPermissionOverride.objects.create(tenant=self.tenant, user=self.wh,
                                              permission=P.EXPORT_DATA, effect=UserPermissionOverride.GRANT)
        UserPermissionOverride.objects.create(tenant=self.tenant, user=self.wh,
                                              permission=P.MANAGE_CUSTOMERS, effect=UserPermissionOverride.GRANT)
        self.client.login(username="gradmin", password="pw")
        self.client.post(f"/users/{self.wh_m.id}/role/", {"role": "SALES"})
        remaining = set(UserPermissionOverride.objects.filter(tenant=self.tenant, user=self.wh)
                        .values_list("permission", flat=True))
        self.assertEqual(remaining, {P.EXPORT_DATA})  # redundant grant pruned, meaningful one kept

    def test_policy_toggle_saves_and_audits(self):
        from core.models import AuditLog
        self.client.login(username="gradmin", password="pw")
        resp = self.client.post("/team/permissions/", {"keep_permissions_on_role_change": "on"})
        self.assertEqual(resp.status_code, 302)
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.keep_permissions_on_role_change)
        self.assertTrue(AuditLog.objects.filter(action="SETTINGS_CHANGED").exists())

    def test_reset_to_role_default(self):
        from core.models import UserPermissionOverride
        from core import permissions as P
        UserPermissionOverride.objects.create(tenant=self.tenant, user=self.wh,
                                              permission=P.EXPORT_DATA, effect=UserPermissionOverride.GRANT)
        self.client.login(username="gradmin", password="pw")
        self.client.post(f"/users/{self.wh_m.id}/permissions/", {"reset": "1"})
        self.assertFalse(UserPermissionOverride.objects.filter(tenant=self.tenant, user=self.wh).exists())


class PermissionMatrixTests(TestCase):
    def test_matrix_helpers(self):
        from core import permissions as P
        self.assertTrue(P.role_has_permission("ADMIN", P.DELETE_RECORDS))
        self.assertTrue(P.role_has_permission("ACCOUNTANT", P.VIEW_FINANCE_REPORTS))
        self.assertFalse(P.role_has_permission("READONLY", P.MANAGE_INVOICES))
        self.assertFalse(P.role_has_permission("SALES", P.EXPORT_DATA))
        self.assertTrue(P.role_has_permission("ADMIN", P.MANAGE_USERS))

    def test_matrix_page_admin_only(self):
        from core.models import OrgMembership
        t = Tenant.objects.create(name="Perm Co")
        admin = User.objects.create_user("permadmin", password="pw")
        OrgMembership.objects.create(user=admin, tenant=t, role="ADMIN", is_default=True)
        sales = User.objects.create_user("permsales", password="pw")
        OrgMembership.objects.create(user=sales, tenant=t, role="SALES", is_default=True)

        c = Client(); c.login(username="permadmin", password="pw")
        resp = c.get("/team/permissions/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Roles &amp; Permissions")
        self.assertContains(resp, "Approve transactions")

        c2 = Client(); c2.login(username="permsales", password="pw")
        self.assertEqual(c2.get("/team/permissions/").status_code, 403)


class TeamInviteTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Invite Co")
        self.admin = User.objects.create_user("invadmin", password="pw")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="invadmin", password="pw")

    def test_invite_creates_user_and_membership_and_emails(self):
        from django.core import mail
        from core.models import OrgMembership
        from django.contrib.auth.models import User as U
        resp = self.client.post("/team/invite/", {"name": "Pat Jones", "email": "pat@team.test", "role": "WAREHOUSE"})
        self.assertEqual(resp.status_code, 302)
        u = U.objects.get(email="pat@team.test")
        self.assertTrue(OrgMembership.objects.filter(user=u, tenant=self.tenant, role="WAREHOUSE").exists())
        self.assertTrue(any("pat@team.test" in m.to for m in mail.outbox))

    def test_invite_requires_admin(self):
        from core.models import OrgMembership
        u = User.objects.create_user("notadmin", password="pw")
        OrgMembership.objects.create(user=u, tenant=self.tenant, role="SALES", is_default=True)
        c = Client(); c.login(username="notadmin", password="pw")
        self.assertEqual(c.get("/team/invite/").status_code, 403)


class CompanyDefaultsTests(TestCase):
    def setUp(self):
        from datetime import date
        from core.models import OrgMembership, Customer, TaxCode
        self.date = date
        self.tenant = Tenant.objects.create(name="Defaults Co", default_payment_terms_days=14,
                                            financial_year_start_month=4)
        self.tenant.default_tax_code = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.tenant.save()
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        self.user = User.objects.create_user("dadmin", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="dadmin", password="pw")

    def test_due_date_defaults_from_payment_terms(self):
        from core.models import CustomerInvoice
        resp = self.client.post("/ar/invoices/new/", {
            "customer": self.customer.id, "invoice_number": "INV-D1",
            "invoice_date": "2026-06-01", "action": "save",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-description": "Item", "lines-0-qty": "1", "lines-0-unit_price": "100",
        })
        self.assertEqual(resp.status_code, 302)
        inv = CustomerInvoice.objects.get(tenant=self.tenant, invoice_number="INV-D1")
        self.assertEqual(inv.due_date, self.date(2026, 6, 15))  # 1 June + 14 days

    def test_supplier_invoice_line_defaults_tax(self):
        resp = self.client.get("/invoices/new/")
        self.assertEqual(resp.status_code, 200)
        fs = resp.context["formset"]
        self.assertEqual(fs.forms[0].initial.get("tax_code"), self.tenant.default_tax_code)

    def test_financial_year_helper(self):
        from core.services import reports
        start, end = reports.current_financial_year(self.tenant, today=self.date(2026, 6, 1))
        self.assertEqual(start, self.date(2026, 4, 1))
        self.assertEqual(end, self.date(2027, 3, 31))

    def test_invoice_shows_branding(self):
        from core.models import CustomerInvoice
        self.tenant.legal_name = "Defaults Co Ltd"
        self.tenant.invoice_footer = "Thanks for your business"
        self.tenant.save()
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-B1")
        resp = self.client.get(f"/ar/invoices/{inv.id}/")
        self.assertContains(resp, "Defaults Co Ltd")
        self.assertContains(resp, "Thanks for your business")


class CsvImportTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Import Co")
        self.user = User.objects.create_user("iadmin", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="iadmin", password="pw")

    def _csv(self, text):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile("data.csv", text.encode("utf-8"), content_type="text/csv")

    def test_import_products_creates_and_reports_errors(self):
        from core.models import Product
        csv_text = "sku,name,standard_cost\nSKU-A,Widget A,2.50\nSKU-B,Gadget B,4\n,Missing SKU,1\n"
        resp = self.client.post("/products/import/", {"file": self._csv(csv_text)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Product.objects.filter(tenant=self.tenant).count(), 2)
        p = Product.objects.get(tenant=self.tenant, sku="SKU-A")
        from decimal import Decimal
        self.assertEqual(p.standard_cost, Decimal("2.50"))
        self.assertContains(resp, "Skipped: 1")

    def test_import_is_upsert(self):
        from core.models import Product
        self.client.post("/products/import/", {"file": self._csv("sku,name\nSKU-X,First\n")})
        self.client.post("/products/import/", {"file": self._csv("sku,name\nSKU-X,Updated Name\n")})
        self.assertEqual(Product.objects.filter(tenant=self.tenant, sku="SKU-X").count(), 1)
        self.assertEqual(Product.objects.get(tenant=self.tenant, sku="SKU-X").name, "Updated Name")

    def test_import_customers_and_suppliers(self):
        from core.models import Customer, Supplier
        self.client.post("/customers/import/", {"file": self._csv("name,email\nAcme Retail,ar@acme.test\n")})
        self.client.post("/suppliers/import/", {"file": self._csv("name,currency_code\nGlobex,USD\n")})
        self.assertTrue(Customer.objects.filter(tenant=self.tenant, name="Acme Retail").exists())
        self.assertEqual(Supplier.objects.get(tenant=self.tenant, name="Globex").currency_code, "USD")

    def test_template_download(self):
        resp = self.client.get("/import/products/template.csv")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv")
        self.assertIn("sku", resp.content.decode())

    def test_import_requires_permission(self):
        # A sales user cannot import products (procurement/admin only).
        from core.models import OrgMembership
        u = User.objects.create_user("salesimp", password="pw")
        OrgMembership.objects.create(user=u, tenant=self.tenant, role="SALES", is_default=True)
        c = Client(); c.login(username="salesimp", password="pw")
        self.assertEqual(c.get("/products/import/").status_code, 403)


class OnboardingTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Onboard Co")
        self.user = User.objects.create_user("oadmin", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="oadmin", password="pw")

    def test_onboarding_page_renders_with_steps(self):
        resp = self.client.get("/onboarding/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Let's get you set up")
        self.assertContains(resp, "First location")

    def test_step_completion_detected(self):
        from core.models import Location
        Location.objects.create(tenant=self.tenant, name="HQ")
        resp = self.client.get("/onboarding/")
        # location step now done -> at least one "Done" badge
        self.assertContains(resp, "Done")

    def test_finish_sets_flag(self):
        resp = self.client.post("/onboarding/finish/")
        self.assertEqual(resp.status_code, 302)
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.onboarding_complete)

    def test_create_new_organisation(self):
        from core.models import Tenant as T, OrgMembership
        resp = self.client.post("/onboarding/new-organisation/", {
            "name": "Fresh Org", "business_type": "LTD", "currency_code": "GBP", "country": "United Kingdom",
        })
        self.assertEqual(resp.status_code, 302)
        org = T.objects.get(name="Fresh Org")
        self.assertTrue(OrgMembership.objects.filter(user=self.user, tenant=org, role="ADMIN").exists())

    def test_dashboard_banner_when_not_onboarded(self):
        resp = self.client.get("/dashboard/admin")
        self.assertContains(resp, "Finish setting up")


class AccessRequestTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Req Co")
        self.admin = User.objects.create_user("adminu", password="pw", email="admin@req.test")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)

    def test_public_can_submit_request(self):
        from core.models import AccessRequest
        resp = self.client.post("/request-access/", {
            "name": "Jane Doe", "employee_id": "E123", "email": "jane@acme.test", "team": "Sales",
        })
        self.assertEqual(resp.status_code, 302)
        r = AccessRequest.objects.get(email="jane@acme.test")
        self.assertEqual(r.status, "PENDING")
        self.assertEqual(r.name, "Jane Doe")

    def test_request_form_public_and_get(self):
        self.assertEqual(self.client.get("/request-access/").status_code, 200)

    def test_list_requires_admin(self):
        staff = User.objects.create_user("sales2", password="pw")
        from core.models import OrgMembership
        OrgMembership.objects.create(user=staff, tenant=self.tenant, role="SALES", is_default=True)
        self.client.login(username="sales2", password="pw")
        self.assertEqual(self.client.get("/access-requests/").status_code, 403)

    def test_admin_approve_creates_account(self):
        from core.models import AccessRequest, OrgMembership
        from django.contrib.auth.models import User as U
        req = AccessRequest.objects.create(name="Bob Lee", email="bob@acme.test", team="Warehouse")
        self.client.login(username="adminu", password="pw")
        resp = self.client.post(f"/access-requests/{req.id}/action/", {"action": "approve", "role": "WAREHOUSE"})
        self.assertEqual(resp.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, "APPROVED")
        self.assertIsNotNone(req.created_user)
        # The created user has a Warehouse membership in this tenant.
        self.assertTrue(OrgMembership.objects.filter(user=req.created_user, tenant=self.tenant, role="WAREHOUSE").exists())
        self.assertEqual(U.objects.filter(username=req.created_user.username).count(), 1)

    def test_admin_reject(self):
        from core.models import AccessRequest
        req = AccessRequest.objects.create(name="Eve", email="eve@acme.test")
        self.client.login(username="adminu", password="pw")
        self.client.post(f"/access-requests/{req.id}/action/", {"action": "reject"})
        req.refresh_from_db()
        self.assertEqual(req.status, "REJECTED")
        self.assertIsNone(req.created_user)

    def test_submit_emails_admin(self):
        from django.core import mail
        self.client.post("/request-access/", {"name": "Jane", "email": "jane@acme.test", "team": "Sales"})
        self.assertTrue(any("admin@req.test" in m.to for m in mail.outbox))

    def test_approve_emails_applicant_with_credentials(self):
        from django.core import mail
        from core.models import AccessRequest
        req = AccessRequest.objects.create(name="Bob", email="bob@acme.test", tenant=self.tenant)
        self.client.login(username="adminu", password="pw")
        self.client.post(f"/access-requests/{req.id}/action/", {"action": "approve", "role": "SALES"})
        applicant_mails = [m for m in mail.outbox if "bob@acme.test" in m.to]
        self.assertTrue(applicant_mails)
        self.assertIn("Temporary password", applicant_mails[-1].body)


class PerOrgEnforcementTests(TestCase):
    """A multi-org user's module access must follow their ACTIVE org's role."""
    def setUp(self):
        from core.models import OrgMembership
        self.org_a = Tenant.objects.create(name="Org A")
        self.org_b = Tenant.objects.create(name="Org B")
        self.user = User.objects.create_user("multi", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.org_a, role="SALES", is_default=True)
        OrgMembership.objects.create(user=self.user, tenant=self.org_b, role="FINANCE")
        self.client.login(username="multi", password="pw")

    def _activate(self, tenant):
        s = self.client.session
        s["active_tenant_id"] = tenant.id
        s.save()

    def test_sales_org_blocked_from_finance_module(self):
        self._activate(self.org_a)  # SALES here
        self.assertEqual(self.client.get("/invoices/").status_code, 403)  # supplier invoices = finance/admin

    def test_finance_org_allowed_finance_module(self):
        self._activate(self.org_b)  # FINANCE here
        self.assertEqual(self.client.get("/invoices/").status_code, 200)

    def test_sales_org_allowed_sales_module(self):
        self._activate(self.org_a)
        self.assertEqual(self.client.get("/sales-orders/").status_code, 200)

    def test_finance_org_blocked_from_sales_only_module(self):
        self._activate(self.org_b)  # FINANCE -> no Sales group
        self.assertEqual(self.client.get("/sales-orders/").status_code, 403)

    def test_group_only_user_still_enforced(self):
        # Legacy user with a Django group but no membership keeps working.
        u = User.objects.create_user("legacy", password="pw")
        u.groups.add(Group.objects.get_or_create(name="Finance")[0])
        c = Client()
        c.login(username="legacy", password="pw")
        self.assertEqual(c.get("/invoices/").status_code, 200)


class PaymentsTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Pay Co")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-P1")
        CustomerInvoiceLine.objects.create(invoice=self.inv, description="Item", qty=Decimal("2"), unit_price=Decimal("100.00"), tax_code=self.std)
        post_customer_invoice(self.inv)  # total 240, status ISSUED

        self.user = User.objects.create_user("pay", password="pw")
        self.user.groups.add(Group.objects.get_or_create(name="Finance")[0])
        UserProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.login(username="pay", password="pw")

    def test_full_receipt_settles_invoice_and_clears_ar(self):
        from core.services import reports
        from core.models import GLAccount, Payment

        resp = self.client.post("/payments/receipts/new/", {
            "customer": self.customer.id, "payment_date": "2026-05-30",
            "amount": "240.00", "method": "BANK", "reference": "FPS-1",
        })
        self.assertEqual(resp.status_code, 302)

        payment = Payment.objects.get(tenant=self.tenant)
        self.assertEqual(payment.status, "POSTED")
        self.assertEqual(payment.allocated, Decimal("240.00"))

        self.inv.refresh_from_db()
        self.assertEqual(self.inv.status, "PAID")

        # AR account nets to zero (invoice DR 240, receipt CR 240).
        balances = reports.account_balances(self.tenant)
        ar = GLAccount.objects.get(tenant=self.tenant, code="1100")
        bank = GLAccount.objects.get(tenant=self.tenant, code="1050")
        self.assertEqual(balances[ar]["balance"], Decimal("0.00"))
        self.assertEqual(balances[bank]["balance"], Decimal("240.00"))

    def test_partial_receipt_leaves_invoice_open(self):
        from core.models import Payment
        self.client.post("/payments/receipts/new/", {
            "customer": self.customer.id, "payment_date": "2026-05-30",
            "amount": "100.00", "method": "BANK", "reference": "FPS-2",
        })
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.status, "ISSUED")
        self.assertEqual(self.inv.outstanding, Decimal("140.00"))

    def test_bank_reconciliation_matches_statement_line(self):
        from core.models import Payment, BankTransaction
        self.client.post("/payments/receipts/new/", {
            "customer": self.customer.id, "payment_date": "2026-05-30",
            "amount": "240.00", "method": "BANK",
        })
        payment = Payment.objects.get(tenant=self.tenant)
        self.assertFalse(payment.is_reconciled)

        # Statement line of the same amount auto-matches to the receipt.
        BankTransaction.objects.create(tenant=self.tenant, description="FPS CREDIT", amount=Decimal("240.00"))
        resp = self.client.post("/bank/reconcile/", {"action": "auto"})
        self.assertEqual(resp.status_code, 302)
        payment.refresh_from_db()
        self.assertTrue(payment.is_reconciled)

    def test_payment_pages_render(self):
        for path in ["/payments/", "/payments/receipts/new/", "/payments/payments/new/", "/bank/reconcile/"]:
            self.assertEqual(self.client.get(path).status_code, 200, path)


class CostingTests(TestCase):
    def setUp(self):
        from core.models import Location
        self.tenant = Tenant.objects.create(name="Cost Co")
        self.product = Product.objects.create(tenant=self.tenant, sku="SKU-C", name="P")
        self.loc = Location.objects.create(tenant=self.tenant, name="WH")

    def test_moving_average_and_outbound_valuation(self):
        from core.services.inventory import apply_movement
        from core.services import reports

        apply_movement(tenant=self.tenant, product=self.product, location=self.loc,
                       movement_type="RECEIVE", qty_delta=Decimal("10"), ref_type="T", ref_id="1",
                       unit_cost=Decimal("2.00"))
        self.product.refresh_from_db()
        self.assertEqual(self.product.average_cost, Decimal("2.0000"))

        apply_movement(tenant=self.tenant, product=self.product, location=self.loc,
                       movement_type="RECEIVE", qty_delta=Decimal("10"), ref_type="T", ref_id="2",
                       unit_cost=Decimal("4.00"))
        self.product.refresh_from_db()
        self.assertEqual(self.product.average_cost, Decimal("3.0000"))  # (20+40)/20

        sale = apply_movement(tenant=self.tenant, product=self.product, location=self.loc,
                              movement_type="SALE", qty_delta=Decimal("-5"), ref_type="T", ref_id="3")
        self.assertEqual(sale.unit_cost, Decimal("3.0000"))
        self.assertEqual(sale.value, Decimal("-15.00"))

        val = reports.stock_valuation(self.tenant)
        self.assertEqual(val["total"], Decimal("45.00"))  # 15 on hand x 3.00

    def test_sales_order_posts_cogs_journal(self):
        from core.services.inventory import apply_movement
        from core.views import _post_sales_order
        from core.models import SalesOrder, SalesOrderLine, JournalEntry

        apply_movement(tenant=self.tenant, product=self.product, location=self.loc,
                       movement_type="RECEIVE", qty_delta=Decimal("10"), ref_type="T", ref_id="1",
                       unit_cost=Decimal("3.00"))
        order = SalesOrder.objects.create(tenant=self.tenant, order_number="SO-C1", ship_from_location=self.loc)
        SalesOrderLine.objects.create(order=order, product=self.product, qty=Decimal("4"), unit_price=Decimal("10.00"))

        _post_sales_order(order)

        je = JournalEntry.objects.get(tenant=self.tenant, ref_type="COGS")
        self.assertEqual(je.total_debit, je.total_credit)
        self.assertEqual(je.total_debit, Decimal("12.00"))  # 4 x 3.00


class FifoCostingTests(TestCase):
    def setUp(self):
        from core.models import Location
        self.tenant = Tenant.objects.create(name="FIFO Co")
        self.product = Product.objects.create(tenant=self.tenant, sku="SKU-F", name="P", cost_method=Product.CostMethod.FIFO)
        self.loc = Location.objects.create(tenant=self.tenant, name="WH")

    def test_fifo_consumes_oldest_layers_first(self):
        from core.services.inventory import apply_movement
        from core.services import reports

        # Layer 1: 10 @ 2.00, Layer 2: 10 @ 5.00
        apply_movement(tenant=self.tenant, product=self.product, location=self.loc,
                       movement_type="RECEIVE", qty_delta=Decimal("10"), ref_type="T", ref_id="1", unit_cost=Decimal("2.00"))
        apply_movement(tenant=self.tenant, product=self.product, location=self.loc,
                       movement_type="RECEIVE", qty_delta=Decimal("10"), ref_type="T", ref_id="2", unit_cost=Decimal("5.00"))

        # Sell 12: 10 @ 2.00 + 2 @ 5.00 = 30.00 COGS (NOT average 3.50*12=42).
        sale = apply_movement(tenant=self.tenant, product=self.product, location=self.loc,
                              movement_type="SALE", qty_delta=Decimal("-12"), ref_type="T", ref_id="3")
        self.assertEqual(sale.value, Decimal("-30.00"))

        # Remaining 8 @ 5.00 = 40.00 on the valuation report.
        val = reports.stock_valuation(self.tenant)
        self.assertEqual(val["total"], Decimal("40.00"))


class LandedCostTests(TestCase):
    def setUp(self):
        from core.models import Location
        self.tenant = Tenant.objects.create(name="Landed Co")
        self.supplier = Supplier.objects.create(tenant=self.tenant, name="S")
        self.product = Product.objects.create(tenant=self.tenant, sku="SKU-L", name="P")
        self.loc = Location.objects.create(tenant=self.tenant, name="WH")
        self.po = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-L", supplier=self.supplier, status=PurchaseOrder.Status.SUBMITTED)
        self.pol = PurchaseOrderLine.objects.create(po=self.po, product=self.product, ordered_qty=Decimal("10"), unit_cost=Decimal("10.00"))
        self.shipment = Shipment.objects.create(tenant=self.tenant, po=self.po, from_supplier=self.supplier, destination=self.loc)
        self.sl = ShipmentLine.objects.create(shipment=self.shipment, po_line=self.pol, expected_qty=Decimal("10"))
        self.user = User.objects.create_user("wh2", password="pw")
        self.user.groups.add(Group.objects.get_or_create(name="Warehouse")[0])
        UserProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.login(username="wh2", password="pw")

    def test_landed_cost_capitalized_and_balanced(self):
        from core.models import JournalEntry, GLAccount
        from core.services import reports

        # Receive 10 @ 10.00 goods (100) + 20.00 freight.
        resp = self.client.post(f"/po/{self.po.id}/receive/", {
            "grn_number": "GRN-L",
            f"recv_{self.sl.id}": "10",
            "landed_cost_name": "Freight",
            "landed_cost_amount": "20.00",
        })
        self.assertEqual(resp.status_code, 302)

        # Inventory capitalized at 120 (100 goods + 20 landed); journal balances.
        je = JournalEntry.objects.get(tenant=self.tenant, ref_type="GRN")
        self.assertEqual(je.total_debit, je.total_credit)
        self.assertEqual(je.total_debit, Decimal("120.00"))

        # Average cost now 12.00 -> valuation 120.
        self.product.refresh_from_db()
        self.assertEqual(self.product.average_cost, Decimal("12.0000"))
        self.assertEqual(reports.stock_valuation(self.tenant)["total"], Decimal("120.00"))


class StandardCostingTests(TestCase):
    def setUp(self):
        from core.models import Location
        self.tenant = Tenant.objects.create(name="Std Co")
        # Standard cost 10.00, but we'll buy at 12.00 -> unfavourable variance.
        self.product = Product.objects.create(
            tenant=self.tenant, sku="SKU-S", name="P",
            cost_method=Product.CostMethod.STANDARD, standard_cost=Decimal("10.00"),
        )
        self.loc = Location.objects.create(tenant=self.tenant, name="WH")
        self.supplier = Supplier.objects.create(tenant=self.tenant, name="S")
        self.po = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-S", supplier=self.supplier, status=PurchaseOrder.Status.SUBMITTED)
        self.pol = PurchaseOrderLine.objects.create(po=self.po, product=self.product, ordered_qty=Decimal("10"), unit_cost=Decimal("12.00"))
        self.shipment = Shipment.objects.create(tenant=self.tenant, po=self.po, from_supplier=self.supplier, destination=self.loc)
        self.sl = ShipmentLine.objects.create(shipment=self.shipment, po_line=self.pol, expected_qty=Decimal("10"))
        self.user = User.objects.create_user("wh3", password="pw")
        self.user.groups.add(Group.objects.get_or_create(name="Warehouse")[0])
        UserProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.login(username="wh3", password="pw")

    def test_receipt_values_at_standard_and_books_variance(self):
        from core.models import JournalEntry, GLAccount
        from core.services import reports

        # Receive 10 @ actual 12.00; standard is 10.00.
        resp = self.client.post(f"/po/{self.po.id}/receive/", {
            "grn_number": "GRN-S", f"recv_{self.sl.id}": "10",
        })
        self.assertEqual(resp.status_code, 302)

        # Inventory carried at standard: 10 x 10.00 = 100.00
        self.assertEqual(reports.stock_valuation(self.tenant)["total"], Decimal("100.00"))

        je = JournalEntry.objects.get(tenant=self.tenant, ref_type="GRN")
        self.assertEqual(je.total_debit, je.total_credit)            # balanced
        self.assertEqual(je.total_credit, Decimal("120.00"))         # GRNI at actual 120
        # Variance = 120 actual - 100 standard = 20 unfavourable (DR PPV).
        ppv = GLAccount.objects.get(tenant=self.tenant, code="5100")
        ppv_line = je.lines.get(account=ppv)
        self.assertEqual(ppv_line.debit, Decimal("20.00"))

    def test_sale_cogs_at_standard(self):
        from core.services.inventory import apply_movement
        from core.views import _post_sales_order
        from core.models import SalesOrder, SalesOrderLine, JournalEntry

        apply_movement(tenant=self.tenant, product=self.product, location=self.loc,
                       movement_type="RECEIVE", qty_delta=Decimal("10"), ref_type="T", ref_id="1", unit_cost=Decimal("12.00"))
        order = SalesOrder.objects.create(tenant=self.tenant, order_number="SO-S1", ship_from_location=self.loc)
        SalesOrderLine.objects.create(order=order, product=self.product, qty=Decimal("4"), unit_price=Decimal("30.00"))
        _post_sales_order(order)

        je = JournalEntry.objects.get(tenant=self.tenant, ref_type="COGS")
        self.assertEqual(je.total_debit, Decimal("40.00"))  # 4 x standard 10.00


class VatReturnTests(TestCase):
    def setUp(self):
        from datetime import date
        from core.models import GoodsReceipt, Location, SupplierInvoice, SupplierInvoiceLine
        self.date = date
        self.tenant = Tenant.objects.create(name="VAT Co")
        std = TaxCode.objects.get(tenant=self.tenant, code="STD")

        # Sales: net 200, output VAT 40
        customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        ci = CustomerInvoice.objects.create(tenant=self.tenant, customer=customer, invoice_number="CINV-1", invoice_date=date(2026, 5, 15))
        CustomerInvoiceLine.objects.create(invoice=ci, description="X", qty=Decimal("2"), unit_price=Decimal("100.00"), tax_code=std)
        post_customer_invoice(ci)

        # Purchases: net 50, input VAT 10
        supplier = Supplier.objects.create(tenant=self.tenant, name="Sup")
        product = Product.objects.create(tenant=self.tenant, sku="SKU-V", name="P")
        loc = Location.objects.create(tenant=self.tenant, name="WH")
        po = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-V", supplier=supplier)
        grn = GoodsReceipt.objects.create(tenant=self.tenant, po=po, grn_number="GRN-V", received_to=loc, status=GoodsReceipt.Status.POSTED)
        si = SupplierInvoice.objects.create(tenant=self.tenant, supplier=supplier, po=po, receipt=grn, invoice_number="SINV-V", invoice_date=date(2026, 5, 16), status="POSTED")
        SupplierInvoiceLine.objects.create(invoice=si, product=product, qty=Decimal("10"), unit_cost=Decimal("5.00"), tax_code=std)

        self.user = User.objects.create_user("vat", password="pw")
        self.user.groups.add(Group.objects.get_or_create(name="Finance")[0])
        UserProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.login(username="vat", password="pw")

    def test_compute_boxes(self):
        from core.services import vat
        b = vat.compute_vat_return(self.tenant, self.date(2026, 5, 1), self.date(2026, 5, 31))
        self.assertEqual(b["box1_vat_due_sales"], Decimal("40.00"))
        self.assertEqual(b["box4_vat_reclaimed"], Decimal("10.00"))
        self.assertEqual(b["box5_net_vat"], Decimal("30.00"))
        self.assertEqual(b["box6_total_sales_ex_vat"], Decimal("200.00"))
        self.assertEqual(b["box7_total_purchases_ex_vat"], Decimal("50.00"))

    def test_save_and_submit(self):
        from core.services import vat
        from core.models import VatReturn
        vr = vat.save_vat_return(self.tenant, self.date(2026, 5, 1), self.date(2026, 5, 31))
        self.assertEqual(vr.box5_net_vat, Decimal("30.00"))
        vat.submit_vat_return(vr)
        vr.refresh_from_db()
        self.assertEqual(vr.status, VatReturn.Status.SUBMITTED)
        self.assertTrue(vr.hmrc_reference.startswith("LOCAL-STUB"))

    def test_vat_pages_render(self):
        self.assertEqual(self.client.get("/vat/").status_code, 200)
        self.assertEqual(self.client.get("/vat/?from=2026-05-01&to=2026-05-31").status_code, 200)


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
                     "/reports/balance-sheet/", "/reports/cash-flow/",
                     "/reports/aged-receivables/", "/reports/aged-payables/"]:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, f"{path} -> {resp.status_code}")


class FinanceExportTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="FX Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-X1")
        CustomerInvoiceLine.objects.create(invoice=inv, description="Item", qty=Decimal("2"), unit_price=Decimal("100.00"), tax_code=self.std)
        post_customer_invoice(inv)
        self.admin = User.objects.create_user("xadmin", password="pw")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)
        self.wh = User.objects.create_user("xwh", password="pw")
        OrgMembership.objects.create(user=self.wh, tenant=self.tenant, role="WAREHOUSE", is_default=True)

    def test_each_kind_exports_csv(self):
        self.client.login(username="xadmin", password="pw")
        kinds = ["trial-balance", "profit-and-loss", "balance-sheet", "cash-flow",
                 "aged-receivables", "aged-payables", "journal", "expenses",
                 "payments", "invoices", "bills", "credit-notes", "bank-transactions"]
        for k in kinds:
            resp = self.client.get(f"/finance/export/{k}.csv")
            self.assertEqual(resp.status_code, 200, f"{k} -> {resp.status_code}")
            self.assertEqual(resp["Content-Type"], "text/csv")

    def test_invoices_export_has_data(self):
        self.client.login(username="xadmin", password="pw")
        body = self.client.get("/finance/export/invoices.csv").content.decode()
        self.assertIn("INV-X1", body)
        self.assertIn("Outstanding", body)

    def test_xlsx_export_returns_workbook(self):
        import io
        from openpyxl import load_workbook
        self.client.login(username="xadmin", password="pw")
        resp = self.client.get("/finance/export/invoices.csv?format=xlsx")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertIn(".xlsx", resp["Content-Disposition"])
        wb = load_workbook(io.BytesIO(resp.content))
        ws = wb.active
        self.assertEqual(ws.cell(row=1, column=1).value, "Number")  # header row
        self.assertTrue(any("INV-X1" in str(c.value) for c in ws["A"]))

    def test_xlsx_export_audited(self):
        from core.models import AuditLog
        self.client.login(username="xadmin", password="pw")
        self.client.get("/finance/export/trial-balance.csv?format=xlsx")
        self.assertTrue(AuditLog.objects.filter(tenant=self.tenant, action="DATA_EXPORTED").exists())

    def test_export_audited(self):
        from core.models import AuditLog
        self.client.login(username="xadmin", password="pw")
        self.client.get("/finance/export/trial-balance.csv")
        self.assertTrue(AuditLog.objects.filter(tenant=self.tenant, action="DATA_EXPORTED").exists())

    def test_export_blocked_without_permission(self):
        self.client.login(username="xwh", password="pw")  # WAREHOUSE lacks export_data
        self.assertEqual(self.client.get("/finance/export/trial-balance.csv").status_code, 403)

    def test_unknown_kind_404(self):
        self.client.login(username="xadmin", password="pw")
        self.assertEqual(self.client.get("/finance/export/nonsense.csv").status_code, 404)


class CashFlowAndGrossProfitTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="CF Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        self.inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-CF1")
        CustomerInvoiceLine.objects.create(invoice=self.inv, description="Item", qty=Decimal("2"), unit_price=Decimal("100.00"), tax_code=self.std)
        post_customer_invoice(self.inv)  # income 200
        self.user = User.objects.create_user("cf", password="pw")
        self.user.groups.add(Group.objects.get_or_create(name="Finance")[0])
        UserProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.login(username="cf", password="pw")

    def test_cogs_account_is_cogs_type_and_gross_profit(self):
        from core.models import GLAccount
        from core.services import reports
        from core.services.gl import post_cogs
        cogs_acc = GLAccount.objects.get(tenant=self.tenant, code="5000")
        self.assertEqual(cogs_acc.type, "COGS")
        post_cogs(self.tenant, Decimal("80.00"), "SALE-1")  # DR COGS 80 / CR inventory 80
        pnl = reports.profit_and_loss(self.tenant)
        self.assertEqual(pnl["income_total"], Decimal("200.00"))
        self.assertEqual(pnl["cogs_total"], Decimal("80.00"))
        self.assertEqual(pnl["gross_profit"], Decimal("120.00"))
        self.assertEqual(pnl["net_profit"], Decimal("120.00"))

    def test_cash_flow_summary_reflects_receipt(self):
        from core.models import Payment, PaymentAllocation, GLAccount
        from core.services.gl import post_payment
        from core.services import reports
        p = Payment.objects.create(tenant=self.tenant, direction=Payment.Direction.RECEIPT,
                                   customer=self.customer, amount=Decimal("240.00"), method="BANK")
        PaymentAllocation.objects.create(payment=p, customer_invoice=self.inv, amount=Decimal("240.00"))
        post_payment(p)
        import datetime
        cf = reports.cash_flow_summary(self.tenant, date_from=datetime.date(2000, 1, 1), date_to=datetime.date(2100, 1, 1))
        self.assertEqual(cf["cash_in"], Decimal("240.00"))
        self.assertEqual(cf["cash_out"], Decimal("0.00"))
        self.assertEqual(cf["net"], Decimal("240.00"))
        ar = GLAccount.objects.get(tenant=self.tenant, code="1100")
        ar_row = next((r for r in cf["rows"] if r["account"] == ar), None)
        self.assertIsNotNone(ar_row)
        self.assertEqual(ar_row["amount"], Decimal("240.00"))


class ExpenseTests(TestCase):
    def setUp(self):
        from core.models import GLAccount
        self.tenant = Tenant.objects.create(name="Exp Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.rent = GLAccount.objects.get(tenant=self.tenant, code="6100")
        self.user = User.objects.create_user("exp", password="pw")
        self.user.groups.add(Group.objects.get_or_create(name="Finance")[0])
        UserProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.login(username="exp", password="pw")

    def _post(self, **overrides):
        data = {
            "expense_date": "2026-05-30", "payee": "Landlord", "category": self.rent.id,
            "description": "Rent", "net_amount": "1000.00", "tax_code": self.std.id,
            "method": "BANK", "reference": "R-1", "paid": "on", "action": "post",
        }
        data.update(overrides)
        return self.client.post("/expenses/new/", data)

    def test_paid_expense_posts_balanced_je(self):
        from core.models import Expense, GLAccount
        from core.services import reports
        resp = self._post()
        self.assertEqual(resp.status_code, 302)
        e = Expense.objects.get(tenant=self.tenant)
        self.assertEqual(e.status, "POSTED")
        self.assertEqual(e.total, Decimal("1200.00"))
        balances = reports.account_balances(self.tenant)
        self.assertEqual(balances[self.rent]["balance"], Decimal("1000.00"))
        vat_in = GLAccount.objects.get(tenant=self.tenant, code="1300")
        bank = GLAccount.objects.get(tenant=self.tenant, code="1050")
        self.assertEqual(balances[vat_in]["balance"], Decimal("200.00"))
        self.assertEqual(balances[bank]["balance"], Decimal("-1200.00"))  # cash out

    def test_unpaid_expense_credits_accounts_payable(self):
        from core.models import GLAccount
        from core.services import reports
        self._post(paid="")  # unchecked -> owed
        balances = reports.account_balances(self.tenant)
        ap = GLAccount.objects.get(tenant=self.tenant, code="2000")
        bank = GLAccount.objects.get(tenant=self.tenant, code="1050")
        self.assertEqual(ap["balance"] if isinstance(ap, dict) else balances[ap]["balance"], Decimal("1200.00"))
        self.assertEqual(balances[bank]["balance"], Decimal("0.00"))

    def test_expense_shows_in_pnl(self):
        from core.services import reports
        self._post()
        pnl = reports.profit_and_loss(self.tenant)
        self.assertEqual(pnl["expense_total"], Decimal("1000.00"))

    def test_draft_then_post(self):
        from core.models import Expense, JournalEntry
        resp = self._post(action="save")
        e = Expense.objects.get(tenant=self.tenant)
        self.assertEqual(e.status, "DRAFT")
        self.assertFalse(JournalEntry.objects.filter(tenant=self.tenant, ref_type="EXPENSE").exists())
        self.client.post(f"/expenses/{e.id}/post/")
        e.refresh_from_db()
        self.assertEqual(e.status, "POSTED")
        self.assertTrue(JournalEntry.objects.filter(tenant=self.tenant, ref_type="EXPENSE").exists())

    def test_category_dropdown_excludes_non_expense_accounts(self):
        resp = self.client.get("/expenses/new/")
        self.assertEqual(resp.status_code, 200)
        form = resp.context["form"]
        codes = {a.code for a in form.fields["category"].queryset}
        self.assertIn("6100", codes)
        self.assertNotIn("1050", codes)  # bank is not an expense category
        self.assertNotIn("4000", codes)  # sales income is not an expense category


class ExpenseEntryCompletenessTests(TestCase):
    def setUp(self):
        from core.models import GLAccount, OrgMembership
        self.tenant = Tenant.objects.create(name="ExpC Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.rent = GLAccount.objects.get(tenant=self.tenant, code="6100")
        self.user = User.objects.create_user("expc", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="FINANCE", is_default=True)
        self.client.login(username="expc", password="pw")

    def _data(self, **overrides):
        data = {
            "expense_date": "2026-06-01", "payee": "X", "category": self.rent.id,
            "net_amount": "100.00", "tax_code": self.std.id, "method": "CARD",
            "action": "save",
        }
        data.update(overrides)
        return data

    def test_new_categories_seeded(self):
        from core.models import GLAccount
        for code, name in [("6150", "Repairs & Maintenance"), ("6250", "Insurance"), ("6450", "Meals & Entertainment")]:
            acc = GLAccount.objects.get(tenant=self.tenant, code=code)
            self.assertEqual(acc.name, name)
            self.assertEqual(acc.type, "EXPENSE")

    def test_reimbursable_flag_saved(self):
        from core.models import Expense
        self.client.post("/expenses/new/", self._data(reimbursable="on"))
        e = Expense.objects.get(tenant=self.tenant)
        self.assertTrue(e.reimbursable)

    def test_receipt_upload_stored_tenant_scoped(self):
        from core.models import Expense
        from django.core.files.uploadedfile import SimpleUploadedFile
        pdf = SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        self.client.post("/expenses/new/", self._data(receipt=pdf))
        e = Expense.objects.get(tenant=self.tenant)
        self.assertTrue(e.receipt)
        self.assertIn(f"expense_receipts/{self.tenant.id}/", e.receipt.name)

    def test_receipt_rejects_bad_type(self):
        from core.models import Expense
        from django.core.files.uploadedfile import SimpleUploadedFile
        bad = SimpleUploadedFile("malware.exe", b"MZ", content_type="application/octet-stream")
        resp = self.client.post("/expenses/new/", self._data(receipt=bad))
        self.assertEqual(resp.status_code, 200)  # re-render with error
        self.assertContains(resp, "Receipt must be a PDF or an image")
        self.assertEqual(Expense.objects.filter(tenant=self.tenant).count(), 0)

    def test_supplier_history_shows_expense(self):
        from core.models import Expense, Supplier
        sup = Supplier.objects.create(tenant=self.tenant, name="Acme")
        Expense.objects.create(tenant=self.tenant, expense_date="2026-06-01", payee="Acme",
                               supplier=sup, category=self.rent, net_amount=Decimal("50"))
        resp = self.client.get(f"/suppliers/{sup.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["expenses"]), 1)
        self.assertTrue(any(t["kind"] == "Expense" for t in resp.context["timeline"]))


class ExpenseApprovalWorkflowTests(TestCase):
    def setUp(self):
        from core.models import GLAccount, OrgMembership
        self.tenant = Tenant.objects.create(name="ExpW Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.rent = GLAccount.objects.get(tenant=self.tenant, code="6100")
        self.staff = User.objects.create_user("staff", password="pw")
        OrgMembership.objects.create(user=self.staff, tenant=self.tenant, role="SALES", is_default=True)
        self.fin = User.objects.create_user("fin", password="pw")
        OrgMembership.objects.create(user=self.fin, tenant=self.tenant, role="FINANCE", is_default=True)

    def _data(self, **o):
        d = {"expense_date": "2026-06-01", "payee": "Cafe", "category": self.rent.id,
             "net_amount": "100.00", "tax_code": self.std.id, "method": "CARD", "action": "submit"}
        d.update(o)
        return d

    def test_staff_submits_for_approval(self):
        from core.models import Expense, JournalEntry
        self.client.login(username="staff", password="pw")
        resp = self.client.post("/expenses/new/", self._data())
        self.assertEqual(resp.status_code, 302)
        e = Expense.objects.get(tenant=self.tenant)
        self.assertEqual(e.status, Expense.Status.SUBMITTED)
        self.assertEqual(e.submitted_by, self.staff)
        self.assertFalse(JournalEntry.objects.filter(tenant=self.tenant, ref_type="EXPENSE").exists())

    def test_staff_cannot_approve(self):
        from core.models import Expense
        e = Expense.objects.create(tenant=self.tenant, expense_date="2026-06-01", payee="X",
                                   category=self.rent, net_amount=Decimal("100"),
                                   status=Expense.Status.SUBMITTED, submitted_by=self.staff)
        self.client.login(username="staff", password="pw")
        resp = self.client.post(f"/expenses/{e.id}/approve/")
        self.assertEqual(resp.status_code, 403)
        e.refresh_from_db()
        self.assertEqual(e.status, Expense.Status.SUBMITTED)

    def test_finance_approves_and_posts(self):
        from core.models import Expense, JournalEntry
        e = Expense.objects.create(tenant=self.tenant, expense_date="2026-06-01", payee="X",
                                   category=self.rent, net_amount=Decimal("100"), tax_code=self.std,
                                   status=Expense.Status.SUBMITTED, submitted_by=self.staff)
        self.client.login(username="fin", password="pw")
        resp = self.client.post(f"/expenses/{e.id}/approve/")
        self.assertEqual(resp.status_code, 302)
        e.refresh_from_db()
        self.assertEqual(e.status, Expense.Status.POSTED)
        self.assertEqual(e.approved_by, self.fin)
        self.assertIsNotNone(e.approved_at)
        self.assertTrue(JournalEntry.objects.filter(tenant=self.tenant, ref_type="EXPENSE", ref_id=str(e.id)).exists())

    def test_finance_rejects(self):
        from core.models import Expense, JournalEntry
        e = Expense.objects.create(tenant=self.tenant, expense_date="2026-06-01", payee="X",
                                   category=self.rent, net_amount=Decimal("100"),
                                   status=Expense.Status.SUBMITTED, submitted_by=self.staff)
        self.client.login(username="fin", password="pw")
        self.client.post(f"/expenses/{e.id}/reject/", {"reason": "No receipt"})
        e.refresh_from_db()
        self.assertEqual(e.status, Expense.Status.REJECTED)
        self.assertEqual(e.rejected_reason, "No receipt")
        self.assertFalse(JournalEntry.objects.filter(tenant=self.tenant, ref_type="EXPENSE").exists())

    def test_finance_direct_post_below_threshold(self):
        from core.models import Expense
        self.client.login(username="fin", password="pw")
        self.client.post("/expenses/new/", self._data(action="post"))
        e = Expense.objects.get(tenant=self.tenant)
        self.assertEqual(e.status, Expense.Status.POSTED)

    def test_threshold_forces_approval_even_for_finance(self):
        from core.models import Expense
        self.tenant.expense_approval_threshold = Decimal("50.00")
        self.tenant.save()
        self.client.login(username="fin", password="pw")
        self.client.post("/expenses/new/", self._data(action="post", net_amount="100.00"))
        e = Expense.objects.get(tenant=self.tenant)
        self.assertEqual(e.status, Expense.Status.SUBMITTED)  # 120 total >= 50 -> needs approval


class BankTransactionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Bank Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-B1")
        CustomerInvoiceLine.objects.create(invoice=inv, description="Item", qty=Decimal("2"), unit_price=Decimal("100.00"), tax_code=self.std)
        post_customer_invoice(inv)
        from core.models import Payment, PaymentAllocation
        from core.services.gl import post_payment
        self.payment = Payment.objects.create(tenant=self.tenant, direction=Payment.Direction.RECEIPT,
                                              customer=self.customer, amount=Decimal("240.00"), method="BANK", reference="FPS-9")
        PaymentAllocation.objects.create(payment=self.payment, customer_invoice=inv, amount=Decimal("240.00"))
        post_payment(self.payment)
        self.user = User.objects.create_user("bk", password="pw")
        self.user.groups.add(Group.objects.get_or_create(name="Finance")[0])
        UserProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.login(username="bk", password="pw")

    def test_import_creates_transactions(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from core.models import BankTransaction
        csv = b"date,description,amount,reference\n2026-06-01,FPS CREDIT,240.00,FPS-9\n2026-06-02,BANK FEE,-5.00,\n"
        resp = self.client.post("/bank/transactions/import/", {"file": SimpleUploadedFile("s.csv", csv, content_type="text/csv")})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(BankTransaction.objects.filter(tenant=self.tenant).count(), 2)

    def test_auto_match_reconciles_exact_amount(self):
        from core.models import BankTransaction
        t = BankTransaction.objects.create(tenant=self.tenant, description="FPS CREDIT", amount=Decimal("240.00"))
        resp = self.client.post("/bank/reconcile/", {"action": "auto"})
        self.assertEqual(resp.status_code, 302)
        t.refresh_from_db()
        self.assertTrue(t.is_reconciled)
        self.assertEqual(t.matched_payment_id, self.payment.id)
        self.payment.refresh_from_db()
        self.assertTrue(self.payment.is_reconciled)

    def test_manual_match_and_unmatch(self):
        from core.models import BankTransaction
        t = BankTransaction.objects.create(tenant=self.tenant, description="FPS CREDIT", amount=Decimal("240.00"))
        self.client.post("/bank/reconcile/", {f"match_{t.id}": f"payment:{self.payment.id}"})
        t.refresh_from_db()
        self.assertTrue(t.is_reconciled)
        self.client.post("/bank/reconcile/", {f"match_{t.id}": ""})
        t.refresh_from_db()
        self.assertFalse(t.is_reconciled)
        self.assertIsNone(t.matched_payment_id)

    def test_reconcile_page_renders_with_book_balance(self):
        from core.models import BankTransaction
        BankTransaction.objects.create(tenant=self.tenant, description="FPS CREDIT", amount=Decimal("240.00"))
        resp = self.client.get("/bank/reconcile/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["book_balance"], Decimal("240.00"))
        self.assertEqual(resp.context["cleared"], Decimal("0.00"))


class CreditNoteTests(TestCase):
    def setUp(self):
        from core.models import GLAccount
        self.tenant = Tenant.objects.create(name="CN Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        self.supplier = Supplier.objects.create(tenant=self.tenant, name="Supp")
        self.inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-CN1")
        CustomerInvoiceLine.objects.create(invoice=self.inv, description="Item", qty=Decimal("2"), unit_price=Decimal("100.00"), tax_code=self.std)
        post_customer_invoice(self.inv)  # total 240
        self.user = User.objects.create_user("cn", password="pw")
        self.user.groups.add(Group.objects.get_or_create(name="Finance")[0])
        UserProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.login(username="cn", password="pw")

    def test_sales_credit_note_reduces_invoice_and_ar(self):
        from core.models import CreditNote, CreditNoteLine, GLAccount
        from core.services.gl import post_credit_note
        from core.services import reports
        cn = CreditNote.objects.create(tenant=self.tenant, kind=CreditNote.Kind.SALES,
                                       credit_note_number="CN-1", customer=self.customer, customer_invoice=self.inv)
        CreditNoteLine.objects.create(credit_note=cn, description="Refund", qty=Decimal("2"), unit_amount=Decimal("100.00"), tax_code=self.std)
        post_credit_note(cn, user=self.user)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.outstanding, Decimal("0.00"))
        self.assertEqual(self.inv.status, "PAID")
        balances = reports.account_balances(self.tenant)
        ar = GLAccount.objects.get(tenant=self.tenant, code="1100")
        self.assertEqual(balances[ar]["balance"], Decimal("0.00"))  # 240 invoice - 240 credit

    def test_sales_credit_drops_invoice_from_aged_receivables(self):
        from core.models import CreditNote, CreditNoteLine
        from core.services.gl import post_credit_note
        from core.services import reports
        self.assertEqual(reports.aged_receivables(self.tenant)["total"], Decimal("240.00"))
        cn = CreditNote.objects.create(tenant=self.tenant, kind=CreditNote.Kind.SALES,
                                       credit_note_number="CN-2", customer=self.customer, customer_invoice=self.inv)
        CreditNoteLine.objects.create(credit_note=cn, description="Refund", qty=Decimal("2"), unit_amount=Decimal("100.00"), tax_code=self.std)
        post_credit_note(cn, user=self.user)
        self.assertEqual(reports.aged_receivables(self.tenant)["total"], Decimal("0.00"))

    def test_purchase_credit_note_posts_balanced_je(self):
        from core.models import CreditNote, CreditNoteLine, GLAccount, JournalEntry
        from core.services.gl import post_credit_note
        from core.services import reports
        acc = GLAccount.objects.get(tenant=self.tenant, code="6900")
        cn = CreditNote.objects.create(tenant=self.tenant, kind=CreditNote.Kind.PURCHASE,
                                       credit_note_number="CN-P1", supplier=self.supplier)
        CreditNoteLine.objects.create(credit_note=cn, description="Overcharge", qty=Decimal("1"), unit_amount=Decimal("100.00"), tax_code=self.std, account=acc)
        je = post_credit_note(cn, user=self.user)
        self.assertEqual(je.total_debit, je.total_credit)  # balanced
        self.assertEqual(je.total_debit, Decimal("120.00"))
        balances = reports.account_balances(self.tenant)
        ap = GLAccount.objects.get(tenant=self.tenant, code="2000")
        self.assertEqual(balances[ap]["balance"], Decimal("-120.00"))  # payable reduced

    def test_create_view_posts_sales_credit(self):
        from core.models import CreditNote
        resp = self.client.post("/credit-notes/new/", {
            "kind": "SALES", "credit_note_number": "CN-V1", "credit_note_date": "2026-06-01",
            "customer": self.customer.id, "customer_invoice": self.inv.id, "action": "post",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-description": "Refund", "lines-0-qty": "1", "lines-0-unit_amount": "50",
            "lines-0-tax_code": self.std.id,
        })
        self.assertEqual(resp.status_code, 302)
        cn = CreditNote.objects.get(tenant=self.tenant, credit_note_number="CN-V1")
        self.assertEqual(cn.status, "POSTED")
        self.assertEqual(cn.total, Decimal("60.00"))


class VatTaxCodeTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="VAT Codes Co")
        self.admin = User.objects.create_user("vcadmin", password="pw")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="vcadmin", password="pw")

    def test_all_five_default_codes_with_treatments(self):
        codes = {c.code: c for c in TaxCode.objects.filter(tenant=self.tenant)}
        self.assertEqual(codes["STD"].kind, "STANDARD")
        self.assertEqual(codes["RED"].kind, "REDUCED")
        self.assertEqual(codes["RED"].rate, Decimal("0.0500"))
        self.assertEqual(codes["ZERO"].kind, "ZERO")
        self.assertEqual(codes["EXEMPT"].kind, "EXEMPT")
        self.assertEqual(codes["OS"].kind, "OUTSIDE")

    def test_in_vat_boxes_excludes_outside_scope(self):
        codes = {c.code: c for c in TaxCode.objects.filter(tenant=self.tenant)}
        self.assertTrue(codes["STD"].in_vat_boxes)
        self.assertTrue(codes["ZERO"].in_vat_boxes)
        self.assertTrue(codes["EXEMPT"].in_vat_boxes)
        self.assertFalse(codes["OS"].in_vat_boxes)

    def test_create_tax_code_audited(self):
        from core.models import AuditLog
        resp = self.client.post("/tax-codes/new/", {
            "code": "CUSTOM", "name": "Custom", "rate": "0.10", "kind": "REDUCED", "is_active": "on",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(TaxCode.objects.filter(tenant=self.tenant, code="CUSTOM").exists())
        self.assertTrue(AuditLog.objects.filter(tenant=self.tenant, action="VAT_RATE_CHANGED").exists())


class VatEngineTests(TestCase):
    def setUp(self):
        from core.models import GLAccount
        self.tenant = Tenant.objects.create(name="VAT Engine Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.os = TaxCode.objects.get(tenant=self.tenant, code="OS")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        self.supplier = Supplier.objects.create(tenant=self.tenant, name="Supp")
        self.expense_acc = GLAccount.objects.get(tenant=self.tenant, code="6900")

        # Sales invoice: 1000 @ standard + 500 outside-scope.
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-V1")
        CustomerInvoiceLine.objects.create(invoice=inv, description="Std", qty=Decimal("1"), unit_price=Decimal("1000.00"), tax_code=self.std)
        CustomerInvoiceLine.objects.create(invoice=inv, description="OutOfScope", qty=Decimal("1"), unit_price=Decimal("500.00"), tax_code=self.os)
        post_customer_invoice(inv)

        # Expense: 200 @ standard (input VAT 40).
        from core.models import Expense
        from core.services.gl import post_expense
        e = Expense.objects.create(tenant=self.tenant, payee="Office", category=self.expense_acc,
                                   net_amount=Decimal("200.00"), tax_code=self.std, paid=True)
        post_expense(e)

        # Sales credit note: 100 @ standard (reduces output).
        from core.models import CreditNote, CreditNoteLine
        from core.services.gl import post_credit_note
        cn = CreditNote.objects.create(tenant=self.tenant, kind=CreditNote.Kind.SALES,
                                       credit_note_number="CN-V1", customer=self.customer)
        CreditNoteLine.objects.create(credit_note=cn, description="Refund", qty=Decimal("1"), unit_amount=Decimal("100.00"), tax_code=self.std)
        post_credit_note(cn)

        # Purchase credit note: 50 @ standard (reduces input).
        cnp = CreditNote.objects.create(tenant=self.tenant, kind=CreditNote.Kind.PURCHASE,
                                        credit_note_number="CN-VP1", supplier=self.supplier)
        CreditNoteLine.objects.create(credit_note=cnp, description="Return", qty=Decimal("1"), unit_amount=Decimal("50.00"), tax_code=self.std, account=self.expense_acc)
        post_credit_note(cnp)

    def _summary(self):
        import datetime
        from core.services import vat
        return vat.vat_summary(self.tenant, datetime.date(2000, 1, 1), datetime.date(2100, 1, 1))

    def test_summary_five_totals(self):
        s = self._summary()
        self.assertEqual(s["vat_on_sales"], Decimal("180.00"))        # 200 invoice - 20 credit
        self.assertEqual(s["total_sales_ex_vat"], Decimal("900.00"))  # 1000 - 100 (OS 500 excluded)
        self.assertEqual(s["vat_reclaimable"], Decimal("30.00"))      # 40 expense - 10 purchase credit
        self.assertEqual(s["total_purchases_ex_vat"], Decimal("150.00"))  # 200 - 50
        self.assertEqual(s["net_vat"], Decimal("150.00"))            # 180 - 30

    def test_outside_scope_excluded_from_box6(self):
        import datetime
        from core.services import vat
        boxes = vat.compute_vat_return(self.tenant, datetime.date(2000, 1, 1), datetime.date(2100, 1, 1))
        self.assertEqual(boxes["box6_total_sales_ex_vat"], Decimal("900.00"))
        self.assertEqual(boxes["box1_vat_due_sales"], Decimal("180.00"))
        self.assertEqual(boxes["box4_vat_reclaimed"], Decimal("30.00"))
        self.assertEqual(boxes["box5_net_vat"], Decimal("150.00"))

    def test_expense_included_in_input_vat(self):
        # Records should include the expense as a PURCHASE with 40 VAT.
        import datetime
        from core.services import vat
        recs = vat.vat_transactions(self.tenant, datetime.date(2000, 1, 1), datetime.date(2100, 1, 1))
        exp = [r for r in recs if r["doc_type"] == "Expense"]
        self.assertEqual(len(exp), 1)
        self.assertEqual(exp[0]["vat"], Decimal("40.00"))

    def test_breakdown_has_rate_groups(self):
        s = self._summary()
        keys = {(b["direction"], b["treatment"]) for b in s["breakdown"]}
        self.assertIn(("SALES", "Standard rate"), keys)
        self.assertIn(("SALES", "Outside the scope of VAT"), keys)
        self.assertIn(("PURCHASE", "Standard rate"), keys)


class VatUiAuditExportTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="VAT UI Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-U1")
        CustomerInvoiceLine.objects.create(invoice=inv, description="Item", qty=Decimal("1"), unit_price=Decimal("1000.00"), tax_code=self.std)
        post_customer_invoice(inv)
        self.admin = User.objects.create_user("vuadmin", password="pw")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="vuadmin", password="pw")

    def test_vat_records_page_renders(self):
        resp = self.client.get("/vat/records/?from=2000-01-01&to=2100-01-01")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "INV-U1")

    def test_save_return_audited(self):
        from core.models import AuditLog
        resp = self.client.post("/vat/save/", {"from": "2000-01-01", "to": "2100-01-01"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(AuditLog.objects.filter(tenant=self.tenant, action="VAT_RETURN_SAVED").exists())

    def test_submit_return_audited(self):
        from core.models import AuditLog, VatReturn
        self.client.post("/vat/save/", {"from": "2000-01-01", "to": "2100-01-01"})
        vr = VatReturn.objects.get(tenant=self.tenant)
        self.client.post(f"/vat/{vr.id}/submit/")
        self.assertTrue(AuditLog.objects.filter(tenant=self.tenant, action="VAT_RETURN_SUBMITTED").exists())

    def test_vat_exports(self):
        for k in ("vat-return", "vat-transactions"):
            resp = self.client.get(f"/finance/export/{k}.csv?from=2000-01-01&to=2100-01-01")
            self.assertEqual(resp.status_code, 200, k)
            self.assertEqual(resp["Content-Type"], "text/csv")
        # the return export contains the box labels
        body = self.client.get("/finance/export/vat-return.csv?from=2000-01-01&to=2100-01-01").content.decode()
        self.assertIn("Net VAT to pay / reclaim", body)

    def test_vat_settings_change_audited(self):
        from core.models import AuditLog
        data = {
            "name": self.tenant.name, "legal_name": "VAT UI Co Ltd", "business_type": "LTD",
            "vat_number": "GB123456789", "vat_registered": "on",
            "address_line1": "1 High St", "address_city": "Manchester", "address_postcode": "M1 2AB",
            "address_country": "United Kingdom", "billing_same_as_business": "on", "billing_country": "United Kingdom",
            "email": "ops@vatui.test", "phone": "+44 20 7946 0000",
            "currency_code": "GBP", "country": "United Kingdom", "timezone": "Europe/London",
            "financial_year_start_month": "4", "default_payment_terms_days": "30", "po_approval_threshold": "0",
        }
        resp = self.client.post("/settings/tenant/", data)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(AuditLog.objects.filter(tenant=self.tenant, action="VAT_SETTINGS_CHANGED").exists())


class InvoiceCompletenessTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Inv Co", default_payment_terms_days=30)
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.tenant.default_tax_code = self.std
        self.tenant.save()
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust", email="cust@example.com")
        self.user = User.objects.create_user("invu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="invu", password="pw")

    def _create(self, number="", action="save", discount="0"):
        return self.client.post("/ar/invoices/new/", {
            "customer": self.customer.id, "invoice_number": number,
            "invoice_date": "2026-06-01", "action": action,
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-description": "Widget", "lines-0-qty": "2", "lines-0-unit_price": "100",
            "lines-0-discount_pct": discount, "lines-0-tax_code": self.std.id,
        })

    def test_invoice_number_auto_generated_when_blank(self):
        self._create(number="")
        inv = CustomerInvoice.objects.get(tenant=self.tenant)
        self.assertEqual(inv.invoice_number, "INV-0001")

    def test_admin_can_override_invoice_number(self):
        self._create(number="CUSTOM-9")
        self.assertTrue(CustomerInvoice.objects.filter(tenant=self.tenant, invoice_number="CUSTOM-9").exists())

    def test_line_discount_applied_to_totals(self):
        self._create(discount="10")  # 2 x 100 = 200, less 10% = 180; VAT 36; total 216
        inv = CustomerInvoice.objects.get(tenant=self.tenant)
        self.assertEqual(inv.subtotal, Decimal("180.00"))
        self.assertEqual(inv.tax_total, Decimal("36.00"))
        self.assertEqual(inv.total, Decimal("216.00"))

    def test_due_date_defaulted_from_terms(self):
        self._create()
        inv = CustomerInvoice.objects.get(tenant=self.tenant)
        self.assertEqual(inv.due_date.isoformat(), "2026-07-01")  # +30 days

    def test_send_marks_sent_and_audits(self):
        from core.models import AuditLog
        self._create(action="issue")
        inv = CustomerInvoice.objects.get(tenant=self.tenant)
        resp = self.client.post(f"/ar/invoices/{inv.id}/send/")
        self.assertEqual(resp.status_code, 302)
        inv.refresh_from_db()
        self.assertEqual(inv.status, "SENT")
        self.assertIsNotNone(inv.sent_at)
        self.assertTrue(AuditLog.objects.filter(tenant=self.tenant, action="INVOICE_SENT").exists())

    def test_cancel_reverses_gl(self):
        from core.services import reports
        from core.models import GLAccount
        self._create(action="issue")
        inv = CustomerInvoice.objects.get(tenant=self.tenant)
        self.client.post(f"/ar/invoices/{inv.id}/cancel/")
        inv.refresh_from_db()
        self.assertEqual(inv.status, "CANCELLED")
        ar = GLAccount.objects.get(tenant=self.tenant, code="1100")
        self.assertEqual(reports.account_balances(self.tenant)[ar]["balance"], Decimal("0.00"))

    def test_partial_payment_shows_partially_paid_and_stays_in_aged(self):
        from core.models import Payment, PaymentAllocation
        from core.services.gl import post_payment
        from core.services import reports
        self._create(action="issue")  # total 240
        inv = CustomerInvoice.objects.get(tenant=self.tenant)
        p = Payment.objects.create(tenant=self.tenant, direction=Payment.Direction.RECEIPT,
                                   customer=self.customer, amount=Decimal("100.00"), method="BANK")
        PaymentAllocation.objects.create(payment=p, customer_invoice=inv, amount=Decimal("100.00"))
        post_payment(p)
        inv.refresh_from_db()
        self.assertEqual(inv.display_status, "Partially paid")
        self.assertEqual(reports.aged_receivables(self.tenant)["total"], Decimal("140.00"))

    def test_overdue_display(self):
        self._create(action="issue")
        inv = CustomerInvoice.objects.get(tenant=self.tenant)
        inv.due_date = inv.invoice_date.replace(year=2000)
        inv.save()
        self.assertEqual(inv.display_status, "Overdue")


class PdfGenerationTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="PDF Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust", email="c@example.com")
        self.inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-PDF1")
        CustomerInvoiceLine.objects.create(invoice=self.inv, description="Widget", qty=Decimal("2"), unit_price=Decimal("100.00"), tax_code=self.std)
        post_customer_invoice(self.inv)
        self.user = User.objects.create_user("pdfu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="pdfu", password="pw")

    def test_invoice_pdf_renders(self):
        resp = self.client.get(f"/ar/invoices/{self.inv.id}/pdf/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertTrue(resp.content[:5] == b"%PDF-")

    def test_credit_note_pdf_renders(self):
        from core.models import CreditNote, CreditNoteLine
        from core.services.gl import post_credit_note
        cn = CreditNote.objects.create(tenant=self.tenant, kind=CreditNote.Kind.SALES,
                                       credit_note_number="CN-PDF1", customer=self.customer)
        CreditNoteLine.objects.create(credit_note=cn, description="Refund", qty=Decimal("1"), unit_amount=Decimal("50.00"), tax_code=self.std)
        post_credit_note(cn)
        resp = self.client.get(f"/credit-notes/{cn.id}/pdf/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.content[:5] == b"%PDF-")

    def test_send_attaches_pdf(self):
        from django.core import mail
        with self.settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            self.client.post(f"/ar/invoices/{self.inv.id}/send/")
            self.assertEqual(len(mail.outbox), 1)
            self.assertTrue(mail.outbox[0].attachments)
            fname, content, mime = mail.outbox[0].attachments[0]
            self.assertEqual(mime, "application/pdf")


class SalesDocumentFlowTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Flow Co", default_payment_terms_days=30)
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.tenant.default_tax_code = self.std
        self.tenant.save()
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust", email="c@example.com")
        self.user = User.objects.create_user("flowu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="flowu", password="pw")

    def _make_quote(self, number=""):
        self.client.post("/quotes/new/", {
            "customer": self.customer.id, "quote_number": number, "quote_date": "2026-06-01",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-description": "Widget", "lines-0-qty": "2", "lines-0-unit_price": "100",
            "lines-0-discount_pct": "0", "lines-0-tax_code": self.std.id,
        })
        from core.models import SalesQuote
        return SalesQuote.objects.get(tenant=self.tenant)

    def test_quote_auto_numbered(self):
        q = self._make_quote()
        self.assertEqual(q.quote_number, "QUO-0001")
        self.assertEqual(q.total, Decimal("240.00"))

    def test_quote_to_order_copies_lines(self):
        from core.models import CustomerOrder
        q = self._make_quote()
        resp = self.client.post(f"/quotes/{q.id}/to-order/")
        self.assertEqual(resp.status_code, 302)
        q.refresh_from_db()
        self.assertEqual(q.status, "CONVERTED")
        order = CustomerOrder.objects.get(tenant=self.tenant)
        self.assertEqual(order.quote_id, q.id)
        self.assertEqual(order.lines.count(), 1)
        self.assertEqual(order.total, Decimal("240.00"))

    def test_quote_to_invoice(self):
        q = self._make_quote()
        self.client.post(f"/quotes/{q.id}/to-invoice/")
        inv = CustomerInvoice.objects.get(tenant=self.tenant)
        self.assertEqual(inv.source_quote_id, q.id)
        self.assertEqual(inv.lines.count(), 1)
        self.assertEqual(inv.invoice_number, "INV-0001")
        self.assertIsNotNone(inv.due_date)

    def test_order_to_invoice(self):
        from core.models import CustomerOrder
        q = self._make_quote()
        self.client.post(f"/quotes/{q.id}/to-order/")
        order = CustomerOrder.objects.get(tenant=self.tenant)
        self.client.post(f"/customer-orders/{order.id}/to-invoice/")
        order.refresh_from_db()
        self.assertEqual(order.status, "INVOICED")
        inv = CustomerInvoice.objects.get(tenant=self.tenant)
        self.assertEqual(inv.source_order_id, order.id)
        self.assertEqual(inv.total, Decimal("240.00"))

    def test_quote_pdf_and_send(self):
        from django.core import mail
        q = self._make_quote()
        self.assertTrue(self.client.get(f"/quotes/{q.id}/pdf/").content[:5] == b"%PDF-")
        with self.settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            self.client.post(f"/quotes/{q.id}/send/")
            self.assertEqual(len(mail.outbox), 1)
            self.assertTrue(mail.outbox[0].attachments)
        q.refresh_from_db()
        self.assertEqual(q.status, "SENT")

    def test_quote_accept(self):
        q = self._make_quote()
        self.client.post(f"/quotes/{q.id}/status/accept/")
        q.refresh_from_db()
        self.assertEqual(q.status, "ACCEPTED")


class RecurringInvoiceTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, RecurringInvoice, RecurringInvoiceLine
        import datetime
        self.tenant = Tenant.objects.create(name="Rec Co", default_payment_terms_days=14)
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        self.tmpl = RecurringInvoice.objects.create(
            tenant=self.tenant, customer=self.customer, name="Monthly retainer",
            frequency="MONTHLY", interval=1, start_date=datetime.date(2026, 1, 1),
            next_run_date=datetime.date(2026, 1, 1), auto_issue=True)
        RecurringInvoiceLine.objects.create(template=self.tmpl, description="Retainer",
                                            qty=Decimal("1"), unit_price=Decimal("500.00"), tax_code=self.std)
        self.user = User.objects.create_user("recu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="recu", password="pw")

    def test_add_months_clamps_day(self):
        import datetime
        from core.services.recurring import add_months
        self.assertEqual(add_months(datetime.date(2026, 1, 31), 1), datetime.date(2026, 2, 28))
        self.assertEqual(add_months(datetime.date(2026, 11, 15), 3), datetime.date(2027, 2, 15))

    def test_generate_catches_up_and_advances(self):
        import datetime
        from core.services import recurring
        # From Jan 1 to Mar 15 2026 -> Jan, Feb, Mar = 3 monthly invoices.
        created = recurring.generate_for_template(self.tmpl, today=datetime.date(2026, 3, 15))
        self.assertEqual(len(created), 3)
        self.tmpl.refresh_from_db()
        self.assertEqual(self.tmpl.occurrences, 3)
        self.assertEqual(self.tmpl.next_run_date, datetime.date(2026, 4, 1))
        # All issued + numbered + due date set from terms.
        for inv in created:
            self.assertEqual(inv.status, "ISSUED")
            self.assertTrue(inv.invoice_number.startswith("INV-"))
            self.assertIsNotNone(inv.due_date)
        self.assertEqual(created[0].total, Decimal("600.00"))  # 500 + 20% VAT

    def test_max_occurrences_deactivates(self):
        import datetime
        from core.services import recurring
        self.tmpl.max_occurrences = 2
        self.tmpl.save()
        created = recurring.generate_for_template(self.tmpl, today=datetime.date(2026, 12, 31))
        self.assertEqual(len(created), 2)
        self.tmpl.refresh_from_db()
        self.assertFalse(self.tmpl.is_active)

    def test_draft_mode_does_not_post(self):
        import datetime
        from core.services import recurring
        self.tmpl.auto_issue = False
        self.tmpl.save()
        created = recurring.generate_for_template(self.tmpl, today=datetime.date(2026, 1, 1))
        self.assertEqual(created[0].status, "DRAFT")

    def test_generate_now_view(self):
        resp = self.client.post(f"/recurring-invoices/{self.tmpl.id}/generate/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(CustomerInvoice.objects.filter(tenant=self.tenant).exists())

    def test_create_view_auto_next_run(self):
        from core.models import RecurringInvoice
        resp = self.client.post("/recurring-invoices/new/", {
            "name": "Weekly", "customer": self.customer.id, "frequency": "WEEKLY", "interval": "1",
            "start_date": "2026-02-01", "next_run_date": "", "auto_issue": "on",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-description": "Svc", "lines-0-qty": "1", "lines-0-unit_price": "10", "lines-0-discount_pct": "0", "lines-0-tax_code": self.std.id,
        })
        self.assertEqual(resp.status_code, 302)
        t = RecurringInvoice.objects.get(tenant=self.tenant, name="Weekly")
        self.assertEqual(t.next_run_date.isoformat(), "2026-02-01")  # defaulted from start


class RefundTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Refund Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        self.inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-R1")
        CustomerInvoiceLine.objects.create(invoice=self.inv, description="Item", qty=Decimal("2"), unit_price=Decimal("100.00"), tax_code=self.std)
        post_customer_invoice(self.inv)  # total 240
        from core.models import Payment, PaymentAllocation
        from core.services.gl import post_payment
        p = Payment.objects.create(tenant=self.tenant, direction=Payment.Direction.RECEIPT,
                                   customer=self.customer, amount=Decimal("240.00"), method="BANK")
        PaymentAllocation.objects.create(payment=p, customer_invoice=self.inv, amount=Decimal("240.00"))
        post_payment(p)
        self.user = User.objects.create_user("refu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="refu", password="pw")

    def test_invoice_refund_sets_status_and_reverses_cash(self):
        from core.models import Payment, GLAccount
        from core.services import reports
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.status, "PAID")
        resp = self.client.post(f"/ar/invoices/{self.inv.id}/refund/")
        self.assertEqual(resp.status_code, 302)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.status, "REFUNDED")
        self.assertEqual(self.inv.display_status, "Refunded")
        refund = Payment.objects.get(tenant=self.tenant, direction="REFUND")
        self.assertEqual(refund.amount, Decimal("240.00"))
        # Bank nets to zero: +240 receipt then -240 refund.
        bank = GLAccount.objects.get(tenant=self.tenant, code="1050")
        self.assertEqual(reports.account_balances(self.tenant)[bank]["balance"], Decimal("0.00"))

    def test_standalone_refund_records_and_audits(self):
        from core.models import AuditLog, Payment
        resp = self.client.post("/payments/refunds/new/", {
            "customer": self.customer.id, "payment_date": "2026-06-01",
            "amount": "50.00", "method": "BANK", "reference": "RFD-1",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Payment.objects.filter(tenant=self.tenant, direction="REFUND", amount=Decimal("50.00")).exists())
        self.assertTrue(AuditLog.objects.filter(tenant=self.tenant, action="REFUND_RECORDED").exists())


class CustomerStatementTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, Payment, PaymentAllocation
        from core.services.gl import post_payment
        import datetime
        self.tenant = Tenant.objects.create(name="Stmt Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust", email="c@example.com")
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-S1",
                                             invoice_date=datetime.date(2026, 3, 1))
        CustomerInvoiceLine.objects.create(invoice=inv, description="Item", qty=Decimal("1"), unit_price=Decimal("100.00"), tax_code=self.std)
        post_customer_invoice(inv)  # 120
        p = Payment.objects.create(tenant=self.tenant, direction=Payment.Direction.RECEIPT, customer=self.customer,
                                   amount=Decimal("50.00"), method="BANK", payment_date=datetime.date(2026, 3, 10))
        PaymentAllocation.objects.create(payment=p, customer_invoice=inv, amount=Decimal("50.00"))
        post_payment(p)
        self.user = User.objects.create_user("stmtu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="stmtu", password="pw")

    def test_statement_running_balance(self):
        import datetime
        from core.services import statements
        data = statements.customer_statement(self.tenant, self.customer,
                                             datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))
        self.assertEqual(data["opening"], Decimal("0.00"))
        self.assertEqual(len(data["rows"]), 2)            # invoice + receipt
        self.assertEqual(data["rows"][-1]["balance"], Decimal("70.00"))  # 120 - 50
        self.assertEqual(data["closing"], Decimal("70.00"))

    def test_opening_balance_excludes_pre_period(self):
        import datetime
        from core.services import statements
        # Period starting after both transactions -> they fall into opening.
        data = statements.customer_statement(self.tenant, self.customer,
                                             datetime.date(2026, 6, 1), datetime.date(2026, 12, 31))
        self.assertEqual(data["opening"], Decimal("70.00"))
        self.assertEqual(len(data["rows"]), 0)
        self.assertEqual(data["closing"], Decimal("70.00"))

    def test_statement_page_and_pdf(self):
        self.assertEqual(self.client.get(f"/customers/{self.customer.id}/statement/").status_code, 200)
        pdf = self.client.get(f"/customers/{self.customer.id}/statement/pdf/")
        self.assertTrue(pdf.content[:5] == b"%PDF-")

    def test_statement_email(self):
        from django.core import mail
        from core.models import AuditLog
        with self.settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            self.client.post(f"/customers/{self.customer.id}/statement/email/")
            self.assertEqual(len(mail.outbox), 1)
            self.assertTrue(mail.outbox[0].attachments)
        self.assertTrue(AuditLog.objects.filter(tenant=self.tenant, action="STATEMENT_SENT").exists())


class SalesReportsTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        import datetime
        self.tenant = Tenant.objects.create(name="SR Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.c1 = Customer.objects.create(tenant=self.tenant, name="Alpha")
        self.c2 = Customer.objects.create(tenant=self.tenant, name="Beta")
        self.p1 = Product.objects.create(tenant=self.tenant, sku="SKU-1", name="Widget")
        self.p2 = Product.objects.create(tenant=self.tenant, sku="SKU-2", name="Gadget")
        self._inv(self.c1, [(self.p1, 2, 100), (self.p2, 1, 50)], "INV-1", datetime.date(2026, 3, 1))
        self._inv(self.c2, [(self.p1, 1, 100)], "INV-2", datetime.date(2026, 3, 2))
        self.user = User.objects.create_user("sru", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="sru", password="pw")

    def _inv(self, customer, lines, number, date):
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=customer, invoice_number=number, invoice_date=date)
        for prod, qty, price in lines:
            CustomerInvoiceLine.objects.create(invoice=inv, product=prod, qty=Decimal(qty), unit_price=Decimal(price), tax_code=self.std)
        post_customer_invoice(inv)
        return inv

    def _range(self):
        import datetime
        return datetime.date(2026, 1, 1), datetime.date(2026, 12, 31)

    def test_history_totals(self):
        from core.services import sales_reports
        d = sales_reports.sales_history(self.tenant, *self._range())
        self.assertEqual(len(d["rows"]), 2)
        self.assertEqual(d["net_total"], Decimal("350.00"))   # 250 + 100
        self.assertEqual(d["grand_total"], Decimal("420.00"))  # +20% VAT

    def test_by_product(self):
        from core.services import sales_reports
        d = sales_reports.sales_by_product(self.tenant, *self._range())
        by_sku = {r["key"]: r for r in d["rows"]}
        self.assertEqual(by_sku["SKU-1"]["qty"], Decimal("3.00"))   # 2 + 1
        self.assertEqual(by_sku["SKU-1"]["net"], Decimal("300.00"))
        self.assertEqual(by_sku["SKU-2"]["net"], Decimal("50.00"))

    def test_by_customer(self):
        from core.services import sales_reports
        d = sales_reports.sales_by_customer(self.tenant, *self._range())
        by_name = {r["name"]: r for r in d["rows"]}
        self.assertEqual(by_name["Alpha"]["total"], Decimal("300.00"))  # (250)*1.2
        self.assertEqual(by_name["Beta"]["total"], Decimal("120.00"))

    def test_by_channel_direct(self):
        from core.services import sales_reports
        d = sales_reports.sales_by_channel(self.tenant, *self._range())
        direct = [r for r in d["rows"] if r["channel"].startswith("Direct")][0]
        self.assertEqual(direct["count"], 2)
        self.assertEqual(direct["total"], Decimal("420.00"))

    def test_report_pages_and_exports(self):
        for path in ["/sales/reports/", "/sales/reports/history/", "/sales/reports/by-product/",
                     "/sales/reports/by-customer/", "/sales/reports/by-channel/"]:
            self.assertEqual(self.client.get(path).status_code, 200, path)
        for k in ["sales-history", "sales-by-product", "sales-by-customer", "sales-by-channel"]:
            r = self.client.get(f"/finance/export/{k}.csv?from=2026-01-01&to=2026-12-31")
            self.assertEqual(r.status_code, 200, k)
            self.assertEqual(r["Content-Type"], "text/csv")


class InvoiceInventoryCogsTests(TestCase):
    def setUp(self):
        from core.models import Location, InventoryMovement, InventoryBalance
        from core.services.inventory import apply_movement
        self.tenant = Tenant.objects.create(name="COGS Co")
        Location.objects.filter(tenant=self.tenant).delete()  # drop auto-seeded Main Location; this class manages its own
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        self.loc = Location.objects.create(tenant=self.tenant, name="Main WH", type=Location.Type.WAREHOUSE)
        self.product = Product.objects.create(tenant=self.tenant, sku="SKU-C1", name="Widget",
                                              cost_method=Product.CostMethod.AVERAGE)
        # Opening stock: 100 @ 4.00 (sets moving-average cost).
        apply_movement(tenant=self.tenant, product=self.product, location=self.loc,
                       movement_type=InventoryMovement.MovementType.RECEIVE, qty_delta=Decimal("100"),
                       ref_type="SEED", ref_id="OPEN", unit_cost=Decimal("4.00"))

    def _invoice(self, with_product=True):
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-C1")
        if with_product:
            CustomerInvoiceLine.objects.create(invoice=inv, product=self.product, qty=Decimal("10"),
                                               unit_price=Decimal("25.00"), tax_code=self.std)
        else:
            CustomerInvoiceLine.objects.create(invoice=inv, description="Consulting", qty=Decimal("1"),
                                               unit_price=Decimal("250.00"), tax_code=self.std)
        post_customer_invoice(inv)
        return inv

    def test_stock_deducted_and_cogs_posted(self):
        from core.models import InventoryBalance, JournalEntry, GLAccount
        from core.services import reports
        self._invoice(with_product=True)
        bal = InventoryBalance.objects.get(tenant=self.tenant, product=self.product, location=self.loc)
        self.assertEqual(bal.on_hand, Decimal("90.00"))  # 100 - 10
        cogs_je = JournalEntry.objects.filter(tenant=self.tenant, ref_type="COGS", ref_id="INV-C1").first()
        self.assertIsNotNone(cogs_je)
        self.assertEqual(cogs_je.total_debit, Decimal("40.00"))  # 10 @ 4.00
        # P&L: revenue 250, COGS 40, gross profit 210.
        pnl = reports.profit_and_loss(self.tenant)
        self.assertEqual(pnl["cogs_total"], Decimal("40.00"))
        self.assertEqual(pnl["gross_profit"], Decimal("210.00"))

    def test_service_line_does_not_touch_stock(self):
        from core.models import JournalEntry, InventoryBalance
        self._invoice(with_product=False)
        self.assertFalse(JournalEntry.objects.filter(tenant=self.tenant, ref_type="COGS").exists())
        bal = InventoryBalance.objects.get(tenant=self.tenant, product=self.product, location=self.loc)
        self.assertEqual(bal.on_hand, Decimal("100.00"))  # untouched

    def test_reissue_does_not_double_post_cogs(self):
        from core.models import JournalEntry
        inv = self._invoice(with_product=True)
        post_customer_invoice(inv)  # idempotent re-call
        self.assertEqual(JournalEntry.objects.filter(tenant=self.tenant, ref_type="COGS", ref_id="INV-C1").count(), 1)

    def test_no_location_skips_cogs(self):
        from core.models import JournalEntry
        # A separate tenant with a product but no stock location at all.
        t2 = Tenant.objects.create(name="NoLoc Co")
        std2 = TaxCode.objects.get(tenant=t2, code="STD")
        cust2 = Customer.objects.create(tenant=t2, name="C2")
        prod2 = Product.objects.create(tenant=t2, sku="SKU-N1", name="Svc")
        inv = CustomerInvoice.objects.create(tenant=t2, customer=cust2, invoice_number="INV-N1")
        CustomerInvoiceLine.objects.create(invoice=inv, product=prod2, qty=Decimal("5"),
                                           unit_price=Decimal("25.00"), tax_code=std2)
        post_customer_invoice(inv)
        self.assertFalse(JournalEntry.objects.filter(tenant=t2, ref_type="COGS").exists())


class SalesEditTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, SalesQuote, SalesQuoteLine
        self.tenant = Tenant.objects.create(name="Edit Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        self.user = User.objects.create_user("editu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="editu", password="pw")

    def _formset(self, prefix_id, desc, qty, price, extra=None):
        data = {
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "1",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-id": str(prefix_id), "lines-0-description": desc, "lines-0-qty": str(qty),
            "lines-0-unit_price": str(price), "lines-0-discount_pct": "0", "lines-0-tax_code": self.std.id,
        }
        if extra:
            data.update(extra)
        return data

    def test_edit_draft_invoice(self):
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-E1")
        line = CustomerInvoiceLine.objects.create(invoice=inv, description="Old", qty=Decimal("1"), unit_price=Decimal("10"), tax_code=self.std)
        data = {"customer": self.customer.id, "invoice_number": "INV-E1", "invoice_date": "2026-06-01", "action": "save"}
        data.update(self._formset(line.id, "New widget", 3, 50))
        resp = self.client.post(f"/ar/invoices/{inv.id}/edit/", data)
        self.assertEqual(resp.status_code, 302)
        inv.refresh_from_db()
        self.assertEqual(inv.subtotal, Decimal("150.00"))
        self.assertEqual(inv.lines.first().description, "New widget")

    def test_cannot_edit_issued_invoice(self):
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-E2")
        CustomerInvoiceLine.objects.create(invoice=inv, description="X", qty=Decimal("1"), unit_price=Decimal("10"), tax_code=self.std)
        post_customer_invoice(inv)
        resp = self.client.get(f"/ar/invoices/{inv.id}/edit/")
        self.assertEqual(resp.status_code, 302)  # redirected away

    def test_edit_draft_quote(self):
        from core.models import SalesQuote, SalesQuoteLine
        q = SalesQuote.objects.create(tenant=self.tenant, customer=self.customer, quote_number="QUO-E1")
        line = SalesQuoteLine.objects.create(quote=q, description="Old", qty=Decimal("1"), unit_price=Decimal("10"), tax_code=self.std)
        data = {"customer": self.customer.id, "quote_number": "QUO-E1", "quote_date": "2026-06-01"}
        data.update(self._formset(line.id, "Revised", 2, 75))
        resp = self.client.post(f"/quotes/{q.id}/edit/", data)
        self.assertEqual(resp.status_code, 302)
        q.refresh_from_db()
        self.assertEqual(q.total, Decimal("180.00"))  # 150 + 20% VAT

    def test_edit_recurring_template(self):
        from core.models import RecurringInvoice, RecurringInvoiceLine
        import datetime
        t = RecurringInvoice.objects.create(tenant=self.tenant, customer=self.customer, name="Old name",
                                            frequency="MONTHLY", interval=1, start_date=datetime.date(2026, 1, 1),
                                            next_run_date=datetime.date(2026, 1, 1))
        line = RecurringInvoiceLine.objects.create(template=t, description="Svc", qty=Decimal("1"), unit_price=Decimal("100"), tax_code=self.std)
        data = {"name": "New name", "customer": self.customer.id, "frequency": "MONTHLY", "interval": "1",
                "start_date": "2026-01-01", "next_run_date": "2026-01-01", "auto_issue": "on"}
        data.update(self._formset(line.id, "Svc", 1, 150))
        resp = self.client.post(f"/recurring-invoices/{t.id}/edit/", data)
        self.assertEqual(resp.status_code, 302)
        t.refresh_from_db()
        self.assertEqual(t.name, "New name")
        self.assertEqual(t.total, Decimal("180.00"))


class SalesHousekeepingTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, SalesQuote, SalesQuoteLine, RecurringInvoice, RecurringInvoiceLine
        import datetime
        self.tenant = Tenant.objects.create(name="HK Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        self.user = User.objects.create_user("hku", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="hku", password="pw")

    def test_expire_quotes(self):
        from core.models import SalesQuote
        from core.services import housekeeping
        import datetime
        past = SalesQuote.objects.create(tenant=self.tenant, customer=self.customer, quote_number="QUO-X1",
                                         status=SalesQuote.Status.SENT, valid_until=datetime.date(2020, 1, 1))
        future = SalesQuote.objects.create(tenant=self.tenant, customer=self.customer, quote_number="QUO-X2",
                                           status=SalesQuote.Status.SENT, valid_until=datetime.date(2999, 1, 1))
        n = housekeeping.expire_quotes(self.tenant)
        self.assertEqual(n, 1)
        past.refresh_from_db(); future.refresh_from_db()
        self.assertEqual(past.status, "EXPIRED")
        self.assertEqual(future.status, "SENT")

    def test_run_for_tenant_throttled_once_per_day(self):
        from core.services import housekeeping
        r1 = housekeeping.run_for_tenant(self.tenant)
        self.assertIsNotNone(r1)
        r2 = housekeeping.run_for_tenant(self.tenant)  # same day -> skipped
        self.assertIsNone(r2)

    def test_run_for_tenant_generates_due_recurring(self):
        from core.models import RecurringInvoice, RecurringInvoiceLine, CustomerInvoice
        from core.services import housekeeping
        import datetime
        from django.utils import timezone
        from core.services.recurring import add_months
        start = add_months(timezone.localdate(), -2)
        t = RecurringInvoice.objects.create(tenant=self.tenant, customer=self.customer, name="Retainer",
                                            frequency="MONTHLY", interval=1, start_date=start, next_run_date=start, auto_issue=True)
        RecurringInvoiceLine.objects.create(template=t, description="Svc", qty=Decimal("1"), unit_price=Decimal("100"), tax_code=self.std)
        res = housekeeping.run_for_tenant(self.tenant, force=True)
        self.assertGreaterEqual(res["generated"], 2)
        self.assertTrue(CustomerInvoice.objects.filter(tenant=self.tenant).exists())

    def test_quote_delete(self):
        from core.models import SalesQuote, AuditLog
        q = SalesQuote.objects.create(tenant=self.tenant, customer=self.customer, quote_number="QUO-D1", status=SalesQuote.Status.DRAFT)
        resp = self.client.post(f"/quotes/{q.id}/delete/")
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(SalesQuote.objects.filter(id=q.id).exists())
        self.assertTrue(AuditLog.objects.filter(tenant=self.tenant, action="QUOTE_DELETED").exists())

    def test_converted_quote_cannot_be_deleted(self):
        from core.models import SalesQuote
        q = SalesQuote.objects.create(tenant=self.tenant, customer=self.customer, quote_number="QUO-D2", status=SalesQuote.Status.CONVERTED)
        self.client.post(f"/quotes/{q.id}/delete/")
        self.assertTrue(SalesQuote.objects.filter(id=q.id).exists())  # blocked

    def test_order_delete_and_invoiced_block(self):
        from core.models import CustomerOrder
        draft = CustomerOrder.objects.create(tenant=self.tenant, customer=self.customer, order_number="SO-D1", status=CustomerOrder.Status.DRAFT)
        self.client.post(f"/customer-orders/{draft.id}/delete/")
        self.assertFalse(CustomerOrder.objects.filter(id=draft.id).exists())
        invoiced = CustomerOrder.objects.create(tenant=self.tenant, customer=self.customer, order_number="SO-D2", status=CustomerOrder.Status.INVOICED)
        self.client.post(f"/customer-orders/{invoiced.id}/delete/")
        self.assertTrue(CustomerOrder.objects.filter(id=invoiced.id).exists())  # blocked

    def test_management_command(self):
        from django.core.management import call_command
        from core.models import SalesQuote
        import datetime
        SalesQuote.objects.create(tenant=self.tenant, customer=self.customer, quote_number="QUO-C1",
                                  status=SalesQuote.Status.SENT, valid_until=datetime.date(2020, 1, 1))
        call_command("run_sales_housekeeping")
        self.assertEqual(SalesQuote.objects.get(quote_number="QUO-C1").status, "EXPIRED")


class CustomerRecordTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Cust Co")
        self.user = User.objects.create_user("custu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="custu", password="pw")

    def test_create_with_all_fields(self):
        resp = self.client.post("/customers/new/", {
            "name": "Acme Wholesale", "customer_type": "WHOLESALE", "status": "ACTIVE",
            "contact_person": "Jo Bloggs", "email": "jo@acme.example", "phone": "+44 20 7946 0000",
            "vat_number": "GB123456789", "company_number": "12345678",
            "billing_address": "1 High St", "shipping_address": "Dock 4",
            "payment_terms_days": "45", "credit_limit": "2500.00", "tags": "VIP, Reseller",
            "notes": "Top account",
        })
        self.assertEqual(resp.status_code, 302)
        c = Customer.objects.get(tenant=self.tenant, name="Acme Wholesale")
        self.assertEqual(c.customer_type, "WHOLESALE")
        self.assertEqual(c.payment_terms_days, 45)
        self.assertEqual(c.credit_limit, Decimal("2500.00"))
        self.assertEqual(c.tag_list, ["VIP", "Reseller"])

    def test_outstanding_balance_and_credit_limit(self):
        from core.services.gl import post_customer_invoice
        std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        c = Customer.objects.create(tenant=self.tenant, name="Limited Co", credit_limit=Decimal("100.00"))
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=c, invoice_number="INV-CL1")
        CustomerInvoiceLine.objects.create(invoice=inv, description="X", qty=Decimal("1"), unit_price=Decimal("200.00"), tax_code=std)
        post_customer_invoice(inv)  # 240 outstanding
        self.assertEqual(c.outstanding_balance, Decimal("240.00"))
        self.assertEqual(c.available_credit, Decimal("-140.00"))
        self.assertTrue(c.is_over_limit)

    def test_import_export_round_trip(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        csv = (b"name,customer_type,contact_person,email,phone,vat_number,company_number,"
               b"billing_address,shipping_address,payment_terms_days,tags\n"
               b"Imported Ltd,TRADE,Sam,sam@imp.example,123,GB1,1122,Addr,Ship,14,Trade\n")
        resp = self.client.post("/customers/import/", {"file": SimpleUploadedFile("c.csv", csv, content_type="text/csv")})
        self.assertEqual(resp.status_code, 200)
        c = Customer.objects.get(tenant=self.tenant, name="Imported Ltd")
        self.assertEqual(c.customer_type, "TRADE")
        self.assertEqual(c.payment_terms_days, 14)
        # export contains the new columns
        body = self.client.get("/export/customers.csv").content.decode()
        self.assertIn("customer_type", body)
        self.assertIn("Imported Ltd", body)


class CustomerProfileTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, Payment, PaymentAllocation, CreditNote, CreditNoteLine
        from core.services.gl import post_customer_invoice, post_payment, post_credit_note
        self.tenant = Tenant.objects.create(name="Prof Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Profile Cust", email="p@example.com",
                                                customer_type="TRADE", credit_limit=Decimal("1000.00"))
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-P1")
        CustomerInvoiceLine.objects.create(invoice=inv, description="Item", qty=Decimal("2"), unit_price=Decimal("100.00"), tax_code=self.std)
        post_customer_invoice(inv)
        p = Payment.objects.create(tenant=self.tenant, direction=Payment.Direction.RECEIPT, customer=self.customer,
                                   amount=Decimal("100.00"), method="BANK")
        PaymentAllocation.objects.create(payment=p, customer_invoice=inv, amount=Decimal("100.00"))
        post_payment(p)
        cn = CreditNote.objects.create(tenant=self.tenant, kind=CreditNote.Kind.SALES, credit_note_number="CN-P1", customer=self.customer)
        CreditNoteLine.objects.create(credit_note=cn, description="Adj", qty=Decimal("1"), unit_amount=Decimal("10.00"), tax_code=self.std)
        post_credit_note(cn)
        self.user = User.objects.create_user("profu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="profu", password="pw")

    def test_profile_page_shows_everything(self):
        resp = self.client.get(f"/customers/{self.customer.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "INV-P1")        # invoice
        self.assertContains(resp, "CN-P1")         # credit note
        self.assertContains(resp, "Receipt")       # payment
        self.assertContains(resp, "Activity timeline")
        # outstanding = invoice 240 - 100 paid = 140 (standalone credit note not allocated to it)
        self.assertEqual(resp.context["c"].outstanding_balance, Decimal("140.00"))

    def test_timeline_is_populated_and_sorted(self):
        resp = self.client.get(f"/customers/{self.customer.id}/")
        timeline = resp.context["timeline"]
        self.assertGreaterEqual(len(timeline), 3)  # invoice + payment + credit note
        dates = [e["date"] for e in timeline]
        self.assertEqual(dates, sorted(dates, reverse=True))


class CustomerSearchDedupTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Search Co")
        self.user = User.objects.create_user("srchu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="srchu", password="pw")
        Customer.objects.create(tenant=self.tenant, name="Alpha Retail", email="a@alpha.example",
                                phone="111", vat_number="GB111", customer_type="TRADE", status="ACTIVE", tags="VIP")
        Customer.objects.create(tenant=self.tenant, name="Beta Wholesale", email="b@beta.example",
                                phone="222", customer_type="WHOLESALE", status="INACTIVE", tags="Bulk")

    def test_search_by_text(self):
        resp = self.client.get("/customers/?q=alpha")
        names = [c.name for c in resp.context["customers"]]
        self.assertEqual(names, ["Alpha Retail"])
        resp = self.client.get("/customers/?q=222")  # phone
        self.assertEqual([c.name for c in resp.context["customers"]], ["Beta Wholesale"])

    def test_filter_by_type_status_tag(self):
        self.assertEqual([c.name for c in self.client.get("/customers/?type=TRADE").context["customers"]], ["Alpha Retail"])
        self.assertEqual([c.name for c in self.client.get("/customers/?status=INACTIVE").context["customers"]], ["Beta Wholesale"])
        self.assertEqual([c.name for c in self.client.get("/customers/?tag=VIP").context["customers"]], ["Alpha Retail"])

    def test_duplicate_detection_blocks_then_confirms(self):
        # New customer reusing Alpha's email -> flagged as duplicate, not saved.
        data = {"name": "Alpha Retail North", "customer_type": "COMPANY", "status": "ACTIVE",
                "email": "a@alpha.example", "credit_limit": "0"}
        resp = self.client.post("/customers/new/", data)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["duplicates"])
        self.assertFalse(Customer.objects.filter(tenant=self.tenant, name="Alpha Retail North").exists())
        # Confirm "save anyway".
        data["confirm_duplicate"] = "1"
        resp = self.client.post("/customers/new/", data)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Customer.objects.filter(tenant=self.tenant, name="Alpha Retail North").exists())

    def test_duplicate_helper_matches_multiple_fields(self):
        from core.views import _find_customer_duplicates
        from core.models import Customer as C
        probe = C(tenant=self.tenant, name="X", email="a@alpha.example", phone="222")
        dups = _find_customer_duplicates(self.tenant, probe)
        matched = {(d["name"], d["match"]) for d in dups}
        self.assertIn(("Alpha Retail", "email"), matched)
        self.assertIn(("Beta Wholesale", "phone"), matched)


class SupplierRecordTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Sup Co")
        self.user = User.objects.create_user("supu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="supu", password="pw")

    def test_create_with_all_fields(self):
        resp = self.client.post("/suppliers/new/", {
            "name": "Globex", "status": "ACTIVE", "currency_code": "GBP", "contact_person": "Pat",
            "email": "p@globex.example", "phone": "111", "vat_number": "GB1", "company_number": "1122",
            "address": "Globex House", "payment_terms_days": "30", "bank_name": "Barclays",
            "bank_account_name": "Globex Ltd", "bank_sort_code": "20-00-00", "bank_account_number": "12345678",
            "categories": "Raw materials, Logistics", "notes": "Primary",
        })
        self.assertEqual(resp.status_code, 302)
        s = Supplier.objects.get(tenant=self.tenant, name="Globex")
        self.assertEqual(s.payment_terms_days, 30)
        self.assertEqual(s.bank_account_number, "12345678")
        self.assertEqual(s.category_list, ["Raw materials", "Logistics"])

    def test_outstanding_payables(self):
        from core.models import PurchaseOrder, GoodsReceipt, Location, SupplierInvoice, SupplierInvoiceLine, Product
        from core.services.gl import post_supplier_invoice
        s = Supplier.objects.create(tenant=self.tenant, name="Bills Co")
        std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        prod = Product.objects.create(tenant=self.tenant, sku="SKU-S1", name="Part")
        loc = Location.objects.create(tenant=self.tenant, name="WH")
        po = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-S1", supplier=s)
        grn = GoodsReceipt.objects.create(tenant=self.tenant, po=po, grn_number="GRN-S1", received_to=loc, status=GoodsReceipt.Status.POSTED)
        inv = SupplierInvoice.objects.create(tenant=self.tenant, supplier=s, po=po, receipt=grn, invoice_number="BILL-1")
        SupplierInvoiceLine.objects.create(invoice=inv, product=prod, qty=Decimal("10"), unit_cost=Decimal("5.00"), tax_code=std)
        post_supplier_invoice(inv)  # 60 owed
        self.assertEqual(s.outstanding_payables, Decimal("60.00"))

    def test_import_export_round_trip(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        csv = (b"name,contact_person,email,phone,vat_number,company_number,address,currency_code,payment_terms_days,categories\n"
               b"Imported Supplies,Sam,sam@imp.example,123,GB9,9988,Addr,GBP,14,Raw materials\n")
        resp = self.client.post("/suppliers/import/", {"file": SimpleUploadedFile("s.csv", csv, content_type="text/csv")})
        self.assertEqual(resp.status_code, 200)
        s = Supplier.objects.get(tenant=self.tenant, name="Imported Supplies")
        self.assertEqual(s.payment_terms_days, 14)
        self.assertEqual(s.contact_person, "Sam")
        body = self.client.get("/export/suppliers.csv").content.decode()
        self.assertIn("contact_person", body)
        self.assertIn("Imported Supplies", body)


class SupplierProfileTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, PurchaseOrder, PurchaseOrderLine, GoodsReceipt, Location, SupplierInvoice, SupplierInvoiceLine
        from core.services.gl import post_supplier_invoice
        self.tenant = Tenant.objects.create(name="SupProf Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.supplier = Supplier.objects.create(tenant=self.tenant, name="Globex", contact_person="Pat", categories="Raw materials")
        self.loc = Location.objects.create(tenant=self.tenant, name="WH")
        self.prod = Product.objects.create(tenant=self.tenant, sku="SKU-PR1", name="Widget")
        po = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-PR1", supplier=self.supplier)
        PurchaseOrderLine.objects.create(po=po, product=self.prod, ordered_qty=Decimal("10"), unit_cost=Decimal("4.00"))
        grn = GoodsReceipt.objects.create(tenant=self.tenant, po=po, grn_number="GRN-PR1", received_to=self.loc, status=GoodsReceipt.Status.POSTED)
        inv = SupplierInvoice.objects.create(tenant=self.tenant, supplier=self.supplier, po=po, receipt=grn, invoice_number="BILL-PR1")
        SupplierInvoiceLine.objects.create(invoice=inv, product=self.prod, qty=Decimal("10"), unit_cost=Decimal("4.00"), tax_code=self.std)
        post_supplier_invoice(inv)
        self.user = User.objects.create_user("spu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="spu", password="pw")

    def test_profile_shows_pos_bills_products_price_history(self):
        resp = self.client.get(f"/suppliers/{self.supplier.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "PO-PR1")
        self.assertContains(resp, "BILL-PR1")
        self.assertContains(resp, "Products supplied")
        self.assertContains(resp, "Price history")
        self.assertContains(resp, "SKU-PR1")
        # products supplied + price history populated
        self.assertEqual(len(resp.context["products_supplied"]), 1)
        self.assertEqual(resp.context["products_supplied"][0]["last_cost"], Decimal("4.00"))
        self.assertGreaterEqual(len(resp.context["price_history"]), 1)
        # outstanding payables = 48 (40 net + 20% VAT)
        self.assertEqual(resp.context["s"].outstanding_payables, Decimal("48.00"))

    def test_timeline_sorted(self):
        resp = self.client.get(f"/suppliers/{self.supplier.id}/")
        dates = [e["date"] for e in resp.context["timeline"]]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertGreaterEqual(len(dates), 2)  # PO + bill


class SupplierDedupPreferredTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="SD Co")
        self.user = User.objects.create_user("sdu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="sdu", password="pw")
        Supplier.objects.create(tenant=self.tenant, name="Globex", email="g@globex.example", phone="111",
                                vat_number="GB1", status="ACTIVE", categories="Raw materials")
        Supplier.objects.create(tenant=self.tenant, name="Acme", email="a@acme.example", phone="222",
                                status="INACTIVE", categories="Logistics")

    def test_search_and_filter(self):
        self.assertEqual([s.name for s in self.client.get("/suppliers/?q=globex").context["suppliers"]], ["Globex"])
        self.assertEqual([s.name for s in self.client.get("/suppliers/?q=222").context["suppliers"]], ["Acme"])
        self.assertEqual([s.name for s in self.client.get("/suppliers/?status=INACTIVE").context["suppliers"]], ["Acme"])
        self.assertEqual([s.name for s in self.client.get("/suppliers/?category=Raw").context["suppliers"]], ["Globex"])

    def test_duplicate_detection(self):
        data = {"name": "Globex North", "status": "ACTIVE", "currency_code": "GBP", "email": "g@globex.example"}
        resp = self.client.post("/suppliers/new/", data)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["duplicates"])
        self.assertFalse(Supplier.objects.filter(tenant=self.tenant, name="Globex North").exists())
        data["confirm_duplicate"] = "1"
        resp = self.client.post("/suppliers/new/", data)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Supplier.objects.filter(tenant=self.tenant, name="Globex North").exists())

    def test_preferred_supplier_link_on_product(self):
        from core.models import OrgMembership
        s = Supplier.objects.get(tenant=self.tenant, name="Globex")
        prod = Product.objects.create(tenant=self.tenant, sku="SKU-PS1", name="Widget")
        # set preferred supplier via the product edit form
        resp = self.client.post(f"/products/{prod.id}/edit/", {
            "sku": "SKU-PS1", "name": "Widget", "product_type": "STOCK", "uom": "each",
            "cost_method": "AVERAGE", "standard_cost": "5.00", "sales_price": "0",
            "reorder_level": "0", "preferred_supplier": s.id,
        })
        self.assertEqual(resp.status_code, 302)
        prod.refresh_from_db()
        self.assertEqual(prod.preferred_supplier_id, s.id)
        # appears under products supplied on the supplier profile
        resp = self.client.get(f"/suppliers/{s.id}/")
        skus = [p["product"].sku for p in resp.context["products_supplied"]]
        self.assertIn("SKU-PS1", skus)


class DetailPageRenderRegressionTests(TestCase):
    """Guards against |default: chains resolving attributes on None FKs."""
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Render Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        self.user = User.objects.create_user("rru", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="rru", password="pw")

    def test_credit_note_detail_renders_when_linked_to_invoice(self):
        from core.models import CreditNote, CreditNoteLine
        from core.services.gl import post_customer_invoice, post_credit_note
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer, invoice_number="INV-R1")
        CustomerInvoiceLine.objects.create(invoice=inv, description="X", qty=Decimal("1"), unit_price=Decimal("100"), tax_code=self.std)
        post_customer_invoice(inv)
        cn = CreditNote.objects.create(tenant=self.tenant, kind=CreditNote.Kind.SALES, credit_note_number="CN-R1",
                                       customer=self.customer, customer_invoice=inv)
        CreditNoteLine.objects.create(credit_note=cn, description="Adj", qty=Decimal("1"), unit_amount=Decimal("10"), tax_code=self.std)
        post_credit_note(cn)
        self.assertEqual(self.client.get(f"/credit-notes/{cn.id}/").status_code, 200)
        self.assertTrue(self.client.get(f"/credit-notes/{cn.id}/pdf/").content[:5] == b"%PDF-")

    def test_recurring_detail_renders_with_productless_line(self):
        from core.models import RecurringInvoice, RecurringInvoiceLine
        import datetime
        t = RecurringInvoice.objects.create(tenant=self.tenant, customer=self.customer, name="Retainer",
                                            frequency="MONTHLY", interval=1, start_date=datetime.date(2026, 1, 1),
                                            next_run_date=datetime.date(2026, 1, 1))
        RecurringInvoiceLine.objects.create(template=t, product=None, description="Support retainer",
                                            qty=Decimal("1"), unit_price=Decimal("300"), tax_code=self.std)
        self.assertEqual(self.client.get(f"/recurring-invoices/{t.id}/").status_code, 200)


class ProductRecordTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, Location
        self.tenant = Tenant.objects.create(name="Prod Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.loc = Location.objects.create(tenant=self.tenant, name="Main WH", type=Location.Type.WAREHOUSE)
        self.user = User.objects.create_user("produ", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="produ", password="pw")

    def _post(self, **over):
        data = {
            "sku": "SKU-A1", "name": "Widget", "product_type": "STOCK", "brand": "Acme",
            "description": "A widget", "is_active": "on", "uom": "each", "sales_price": "19.99",
            "tax_code": self.std.id, "cost_method": "AVERAGE", "standard_cost": "8.00",
            "reorder_level": "10", "barcode": "5012345678900",
            "opening_stock": "50", "opening_location": self.loc.id,
        }
        data.update(over)
        return self.client.post("/products/new/", data)

    def test_create_with_fields_and_opening_stock(self):
        from core.models import Product, InventoryBalance, ProductBarcode
        resp = self._post()
        self.assertEqual(resp.status_code, 302)
        p = Product.objects.get(tenant=self.tenant, sku="SKU-A1")
        self.assertEqual(p.product_type, "STOCK")
        self.assertEqual(p.sales_price, Decimal("19.99"))
        self.assertEqual(p.tax_code_id, self.std.id)
        self.assertTrue(ProductBarcode.objects.filter(tenant=self.tenant, code="5012345678900").exists())
        bal = InventoryBalance.objects.get(tenant=self.tenant, product=p, location=self.loc)
        self.assertEqual(bal.on_hand, Decimal("50.00"))
        # margin = 19.99 - 8.00 cost
        self.assertEqual(p.margin, Decimal("11.99"))

    def test_duplicate_sku_friendly_error(self):
        self._post()
        resp = self._post(barcode="")  # same SKU
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "SKU already exists")

    def test_duplicate_barcode_friendly_error(self):
        self._post()
        resp = self._post(sku="SKU-A2")  # same barcode
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already assigned to another product")

    def test_import_creates_category_and_subcategory(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from core.models import Product, ProductCategory
        csv = (b"sku,name,product_type,category,brand,description,uom,cost_method,standard_cost,sales_price,is_active,barcode\n"
               b"SKU-IMP1,Imported Cam,STOCK,Electronics / Webcams,Acme,HD cam,each,AVERAGE,9.99,19.99,yes,5099999999999\n")
        resp = self.client.post("/products/import/", {"file": SimpleUploadedFile("p.csv", csv, content_type="text/csv")})
        self.assertEqual(resp.status_code, 200)
        p = Product.objects.get(tenant=self.tenant, sku="SKU-IMP1")
        self.assertEqual(p.product_type, "STOCK")
        self.assertEqual(p.sales_price, Decimal("19.99"))
        self.assertEqual(str(p.category), "Electronics / Webcams")
        self.assertTrue(ProductCategory.objects.filter(tenant=self.tenant, name="Webcams", parent__name="Electronics").exists())
        body = self.client.get("/export/products.csv").content.decode()
        self.assertIn("product_type", body)
        self.assertIn("SKU-IMP1", body)


class ProductDetailTests(TestCase):
    def setUp(self):
        from core.models import (OrgMembership, Location, InventoryMovement, PurchaseOrder,
                                 PurchaseOrderLine, Supplier)
        from core.services.inventory import apply_movement
        from core.services.gl import post_customer_invoice
        self.tenant = Tenant.objects.create(name="PD Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.loc = Location.objects.create(tenant=self.tenant, name="Main WH", type=Location.Type.WAREHOUSE)
        self.supplier = Supplier.objects.create(tenant=self.tenant, name="Globex")
        self.product = Product.objects.create(tenant=self.tenant, sku="SKU-PD1", name="Widget",
                                              sales_price=Decimal("20.00"), standard_cost=Decimal("8.00"),
                                              preferred_supplier=self.supplier, reorder_level=Decimal("10"))
        # opening stock + cost
        apply_movement(tenant=self.tenant, product=self.product, location=self.loc,
                       movement_type=InventoryMovement.MovementType.RECEIVE, qty_delta=Decimal("100"),
                       ref_type="SEED", ref_id="X", unit_cost=Decimal("8.00"))
        # a PO line (purchase + price history)
        po = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-PD1", supplier=self.supplier)
        PurchaseOrderLine.objects.create(po=po, product=self.product, ordered_qty=Decimal("50"), unit_cost=Decimal("7.50"))
        from core.services.purchasing import record_po_prices
        record_po_prices(po)  # populates supplier price history
        # a sale
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=Customer.objects.create(tenant=self.tenant, name="C"), invoice_number="INV-PD1")
        CustomerInvoiceLine.objects.create(invoice=inv, product=self.product, qty=Decimal("5"), unit_price=Decimal("20.00"), tax_code=self.std)
        post_customer_invoice(inv)  # deducts 5 from stock + records sale
        self.user = User.objects.create_user("pdu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="pdu", password="pw")

    def test_detail_page_renders_all_sections(self):
        resp = self.client.get(f"/products/{self.product.id}/")
        self.assertEqual(resp.status_code, 200)
        for needle in ["Stock by location", "Sales history", "Purchase history",
                       "Price history", "Stock movements", "Suppliers",
                       "INV-PD1", "PO-PD1", "Globex", "Main WH"]:
            self.assertContains(resp, needle)

    def test_detail_aggregates(self):
        resp = self.client.get(f"/products/{self.product.id}/")
        self.assertEqual(resp.context["qty_sold"], Decimal("5.00"))
        self.assertEqual(resp.context["revenue"], Decimal("100.00"))
        self.assertEqual(resp.context["qty_purchased"], Decimal("50.00"))
        self.assertEqual(len(resp.context["price_history"]), 1)
        # margin: 20 sales - 8 avg cost = 12
        self.assertEqual(resp.context["p"].margin, Decimal("12.00"))
        # on hand: 100 received - 5 sold = 95
        self.assertEqual(resp.context["p"].on_hand_total, Decimal("95.00"))


class ProductCategorySearchTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, ProductCategory
        self.tenant = Tenant.objects.create(name="PC Co")
        self.user = User.objects.create_user("pcu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="pcu", password="pw")
        self.elec = ProductCategory.objects.create(tenant=self.tenant, name="Electronics")
        Product.objects.create(tenant=self.tenant, sku="SKU-EL1", name="Webcam", brand="Acme",
                               product_type="STOCK", category=self.elec, is_active=True)
        Product.objects.create(tenant=self.tenant, sku="SKU-SV1", name="Setup service", brand="SwifPro",
                               product_type="SERVICE", is_active=False)

    def test_search_and_filters(self):
        self.assertEqual([p.sku for p in self.client.get("/products/?q=webcam").context["products"]], ["SKU-EL1"])
        self.assertEqual([p.sku for p in self.client.get("/products/?q=acme").context["products"]], ["SKU-EL1"])
        self.assertEqual([p.sku for p in self.client.get("/products/?type=SERVICE").context["products"]], ["SKU-SV1"])
        self.assertEqual([p.sku for p in self.client.get(f"/products/?category={self.elec.id}").context["products"]], ["SKU-EL1"])
        self.assertEqual([p.sku for p in self.client.get("/products/?status=inactive").context["products"]], ["SKU-SV1"])

    def test_category_create_and_subcategory_and_delete(self):
        from core.models import ProductCategory
        # create a top-level
        self.client.post("/product-categories/", {"name": "Office"})
        office = ProductCategory.objects.get(tenant=self.tenant, name="Office")
        # create a subcategory under it
        self.client.post("/product-categories/", {"name": "Stationery", "parent": office.id})
        sub = ProductCategory.objects.get(tenant=self.tenant, name="Stationery")
        self.assertEqual(sub.parent_id, office.id)
        # page renders
        self.assertEqual(self.client.get("/product-categories/").status_code, 200)
        # delete: products keep data (FK SET_NULL)
        self.client.post(f"/product-categories/{self.elec.id}/delete/")
        self.assertFalse(ProductCategory.objects.filter(id=self.elec.id).exists())
        self.assertTrue(Product.objects.filter(sku="SKU-EL1").exists())

    def test_category_parent_choices_exclude_subcategories(self):
        from core.forms import ProductCategoryForm
        from core.models import ProductCategory
        from core.current import set_current_tenant
        set_current_tenant(self.tenant)
        sub = ProductCategory.objects.create(tenant=self.tenant, name="Cameras", parent=self.elec)
        form = ProductCategoryForm()
        parents = list(form.fields["parent"].queryset)
        self.assertIn(self.elec, parents)
        self.assertNotIn(sub, parents)  # subcategories can't be parents
        set_current_tenant(None)


class InventoryLedgerFoundationTests(TestCase):
    def test_movement_records_user_and_new_types_exist(self):
        from core.models import InventoryMovement, Location, Product
        from core.services.inventory import apply_movement
        t = Tenant.objects.create(name="Ledger Co")
        u = User.objects.create_user("ledu", password="pw")
        loc = Location.objects.create(tenant=t, name="Van A", type=Location.Type.VAN)
        p = Product.objects.create(tenant=t, sku="SKU-L1", name="Item")
        m = apply_movement(tenant=t, product=p, location=loc,
                           movement_type=InventoryMovement.MovementType.DAMAGE,
                           qty_delta=Decimal("-2"), ref_type="ADJ", ref_id="1",
                           notes="broken", user=u)
        self.assertEqual(m.user_id, u.id)
        self.assertEqual(m.movement_type, "DAMAGE")
        # new movement types + location types are registered
        mt = dict(InventoryMovement.MovementType.choices)
        for k in ("DAMAGE", "WRITE_OFF", "RETURN_SUPPLIER"):
            self.assertIn(k, mt)
        lt = dict(Location.Type.choices)
        for k in ("OFFICE", "VAN", "POPUP"):
            self.assertIn(k, lt)


class StockAdjustmentTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, Location, InventoryMovement
        from core.services.inventory import apply_movement
        self.tenant = Tenant.objects.create(name="Adj Co", stock_adjustment_approval_threshold=Decimal("100.00"))
        self.loc = Location.objects.create(tenant=self.tenant, name="WH", type=Location.Type.WAREHOUSE)
        self.product = Product.objects.create(tenant=self.tenant, sku="SKU-AJ1", name="Widget", standard_cost=Decimal("10.00"))
        apply_movement(tenant=self.tenant, product=self.product, location=self.loc,
                       movement_type=InventoryMovement.MovementType.RECEIVE, qty_delta=Decimal("100"),
                       ref_type="SEED", ref_id="X", unit_cost=Decimal("10.00"))
        self.user = User.objects.create_user("aju", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="aju", password="pw")

    def _bal(self):
        from core.models import InventoryBalance
        return InventoryBalance.objects.get(tenant=self.tenant, product=self.product, location=self.loc).on_hand

    def test_small_adjustment_auto_posts(self):
        from core.models import StockAdjustment, InventoryMovement
        # 3 units @ 10 = 30 < 100 threshold -> posts immediately
        resp = self.client.post("/inventory/adjustments/new/", {
            "product": self.product.id, "location": self.loc.id, "reason": "DAMAGE",
            "qty_delta": "-3", "notes": "broken"})
        self.assertEqual(resp.status_code, 302)
        adj = StockAdjustment.objects.get(tenant=self.tenant)
        self.assertEqual(adj.status, "POSTED")
        self.assertEqual(self._bal(), Decimal("97.00"))
        m = InventoryMovement.objects.filter(tenant=self.tenant, movement_type="DAMAGE").first()
        self.assertEqual(m.user_id, self.user.id)

    def test_large_adjustment_needs_approval(self):
        from core.models import StockAdjustment
        # 20 @ 10 = 200 >= 100 -> pending, not posted
        self.client.post("/inventory/adjustments/new/", {
            "product": self.product.id, "location": self.loc.id, "reason": "WRITE_OFF",
            "qty_delta": "-20", "notes": "lost"})
        adj = StockAdjustment.objects.get(tenant=self.tenant)
        self.assertEqual(adj.status, "PENDING")
        self.assertEqual(self._bal(), Decimal("100.00"))  # unchanged
        # approve -> posts
        self.client.post(f"/inventory/adjustments/{adj.id}/approve/")
        adj.refresh_from_db()
        self.assertEqual(adj.status, "POSTED")
        self.assertEqual(adj.approved_by_id, self.user.id)
        self.assertEqual(self._bal(), Decimal("80.00"))

    def test_reject_does_not_post(self):
        from core.models import StockAdjustment
        self.client.post("/inventory/adjustments/new/", {
            "product": self.product.id, "location": self.loc.id, "reason": "WRITE_OFF",
            "qty_delta": "-50", "notes": "x"})
        adj = StockAdjustment.objects.get(tenant=self.tenant)
        self.client.post(f"/inventory/adjustments/{adj.id}/reject/")
        adj.refresh_from_db()
        self.assertEqual(adj.status, "REJECTED")
        self.assertEqual(self._bal(), Decimal("100.00"))

    def test_zero_qty_rejected(self):
        resp = self.client.post("/inventory/adjustments/new/", {
            "product": self.product.id, "location": self.loc.id, "reason": "ADJUSTMENT", "qty_delta": "0"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "cannot be zero")


class LowStockAndLedgerTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, Location, InventoryMovement
        from core.services.inventory import apply_movement
        self.tenant = Tenant.objects.create(name="LS Co")
        Location.objects.filter(tenant=self.tenant).delete()  # WH is the only (auto-selected) site
        self.loc = Location.objects.create(tenant=self.tenant, name="WH", type=Location.Type.WAREHOUSE)
        self.user = User.objects.create_user("lsu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="lsu", password="pw")
        # low: reorder 50, on hand 10
        self.low = Product.objects.create(tenant=self.tenant, sku="SKU-LOW", name="Low", reorder_level=Decimal("50"))
        apply_movement(tenant=self.tenant, product=self.low, location=self.loc, movement_type="RECEIVE",
                       qty_delta=Decimal("10"), ref_type="SEED", ref_id="1", unit_cost=Decimal("1"), user=self.user)
        # ok: reorder 5, on hand 100
        self.ok = Product.objects.create(tenant=self.tenant, sku="SKU-OK", name="Ok", reorder_level=Decimal("5"))
        apply_movement(tenant=self.tenant, product=self.ok, location=self.loc, movement_type="RECEIVE",
                       qty_delta=Decimal("100"), ref_type="SEED", ref_id="2", unit_cost=Decimal("1"))

    def test_low_stock_lists_only_below_reorder(self):
        resp = self.client.get("/inventory/low-stock/")
        self.assertEqual(resp.status_code, 200)
        skus = [r["product"].sku for r in resp.context["rows"]]
        self.assertEqual(skus, ["SKU-LOW"])
        self.assertEqual(resp.context["rows"][0]["shortfall"], Decimal("40.00"))

    def test_stock_movements_ledger_and_filter(self):
        resp = self.client.get("/inventory/movements/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["movements"]), 2)
        # filter by product
        resp = self.client.get(f"/inventory/movements/?product={self.low.id}")
        self.assertEqual([m.product.sku for m in resp.context["movements"]], ["SKU-LOW"])
        # filter by type
        resp = self.client.get("/inventory/movements/?type=RECEIVE")
        self.assertEqual(len(resp.context["movements"]), 2)
        # the ledger shows the user who made the movement
        self.assertContains(self.client.get("/inventory/movements/"), "lsu")


class POCompletenessTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, Supplier, Location, GoodsReceipt, SupplierInvoice, SupplierInvoiceLine
        self.tenant = Tenant.objects.create(name="POC Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.supplier = Supplier.objects.create(tenant=self.tenant, name="Globex", email="g@globex.example", address="Globex House")
        self.loc = Location.objects.create(tenant=self.tenant, name="WH", type=Location.Type.WAREHOUSE)
        self.prod = Product.objects.create(tenant=self.tenant, sku="SKU-PO1", name="Part")
        self.user = User.objects.create_user("pocu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="pocu", password="pw")

    def test_po_line_vat_and_totals(self):
        from core.models import PurchaseOrder, PurchaseOrderLine
        po = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-1", supplier=self.supplier, delivery_address="Dock 4")
        PurchaseOrderLine.objects.create(po=po, product=self.prod, ordered_qty=Decimal("10"), unit_cost=Decimal("5.00"), tax_code=self.std)
        self.assertEqual(po.subtotal, Decimal("50.00"))
        self.assertEqual(po.tax_total, Decimal("10.00"))
        self.assertEqual(po.total, Decimal("60.00"))

    def test_po_pdf_renders(self):
        from core.models import PurchaseOrder, PurchaseOrderLine
        po = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-2", supplier=self.supplier)
        PurchaseOrderLine.objects.create(po=po, product=self.prod, ordered_qty=Decimal("3"), unit_cost=Decimal("5.00"), tax_code=self.std)
        resp = self.client.get(f"/po/{po.id}/pdf/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.content[:5] == b"%PDF-")

    def test_bill_posting_sets_po_billed(self):
        from core.models import PurchaseOrder, GoodsReceipt, SupplierInvoice, SupplierInvoiceLine
        from core.services.gl import post_supplier_invoice
        po = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-3", supplier=self.supplier, status=PurchaseOrder.Status.RECEIVED)
        grn = GoodsReceipt.objects.create(tenant=self.tenant, po=po, grn_number="GRN-3", received_to=self.loc, status=GoodsReceipt.Status.POSTED)
        inv = SupplierInvoice.objects.create(tenant=self.tenant, supplier=self.supplier, po=po, receipt=grn, invoice_number="BILL-3")
        SupplierInvoiceLine.objects.create(invoice=inv, product=self.prod, qty=Decimal("10"), unit_cost=Decimal("5.00"), tax_code=self.std)
        post_supplier_invoice(inv)
        po.refresh_from_db()
        self.assertEqual(po.status, "BILLED")

    def test_po_email_attaches_pdf(self):
        from django.core import mail
        from core.models import PurchaseOrder, PurchaseOrderLine
        po = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-4", supplier=self.supplier, status=PurchaseOrder.Status.APPROVED)
        PurchaseOrderLine.objects.create(po=po, product=self.prod, ordered_qty=Decimal("2"), unit_cost=Decimal("5.00"), tax_code=self.std)
        with self.settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            self.client.post(f"/po/{po.id}/send/")
            self.assertEqual(len(mail.outbox), 1)
            self.assertTrue(mail.outbox[0].attachments)
            self.assertEqual(mail.outbox[0].attachments[0][2], "application/pdf")

    def test_backorders_view(self):
        from core.models import PurchaseOrder, PurchaseOrderLine
        po = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-5", supplier=self.supplier, status=PurchaseOrder.Status.PARTIALLY_RECEIVED)
        PurchaseOrderLine.objects.create(po=po, product=self.prod, ordered_qty=Decimal("10"), received_qty=Decimal("4"), unit_cost=Decimal("5.00"))
        resp = self.client.get("/po/backorders/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["rows"]), 1)
        self.assertEqual(resp.context["rows"][0]["open_qty"], Decimal("6.00"))


class PurchaseRequisitionTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Req Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.supplier = Supplier.objects.create(tenant=self.tenant, name="Globex", email="g@globex.example")
        self.loc = Location.objects.create(tenant=self.tenant, name="WH", type=Location.Type.WAREHOUSE)
        self.prod = Product.objects.create(tenant=self.tenant, sku="SKU-R1", name="Part",
                                           standard_cost=Decimal("3.00"), preferred_supplier=self.supplier)
        from core.models import Department
        self.dept = Department.objects.create(tenant=self.tenant, name="Ops")
        self.user = User.objects.create_user("requ", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="requ", password="pw")

    def _make_req(self, status=None):
        from core.models import PurchaseRequisition, PurchaseRequisitionLine
        req = PurchaseRequisition.objects.create(
            tenant=self.tenant, req_number="PR-T1", preferred_supplier=self.supplier,
            status=status or PurchaseRequisition.Status.DRAFT,
        )
        PurchaseRequisitionLine.objects.create(
            requisition=req, product=self.prod, quantity=Decimal("10"), estimated_unit_cost=Decimal("4.00"),
        )
        return req

    def test_create_via_view(self):
        from core.models import PurchaseRequisition
        resp = self.client.post("/requisitions/new/", {
            "department": self.dept.id, "preferred_supplier": self.supplier.id, "needed_by": "",
            "justification": "Restock", "action": "submit",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": self.prod.id, "lines-0-quantity": "5",
            "lines-0-estimated_unit_cost": "4.00", "lines-0-notes": "",
        })
        self.assertEqual(resp.status_code, 302)
        req = PurchaseRequisition.objects.get(tenant=self.tenant)
        self.assertEqual(req.status, PurchaseRequisition.Status.SUBMITTED)
        self.assertEqual(req.department, self.dept)
        self.assertEqual(req.requested_by, self.user)
        self.assertEqual(req.lines.count(), 1)
        self.assertEqual(req.estimated_total, Decimal("20.00"))

    def test_submit_and_approve(self):
        from core.models import PurchaseRequisition
        req = self._make_req()
        self.assertEqual(self.client.post(f"/requisitions/{req.id}/submit/").status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, PurchaseRequisition.Status.SUBMITTED)
        self.assertEqual(self.client.post(f"/requisitions/{req.id}/approve/").status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, PurchaseRequisition.Status.APPROVED)
        self.assertEqual(req.approved_by, self.user)

    def test_reject(self):
        from core.models import PurchaseRequisition
        req = self._make_req(status=PurchaseRequisition.Status.SUBMITTED)
        self.client.post(f"/requisitions/{req.id}/reject/", {"reason": "Over budget"})
        req.refresh_from_db()
        self.assertEqual(req.status, PurchaseRequisition.Status.REJECTED)
        self.assertEqual(req.rejected_reason, "Over budget")

    def test_convert_to_po_carries_lines(self):
        from core.models import PurchaseRequisition, PurchaseOrder
        req = self._make_req(status=PurchaseRequisition.Status.APPROVED)
        resp = self.client.post(f"/requisitions/{req.id}/convert/")
        self.assertEqual(resp.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, PurchaseRequisition.Status.CONVERTED)
        self.assertIsNotNone(req.converted_po)
        po = req.converted_po
        self.assertEqual(po.status, PurchaseOrder.Status.DRAFT)
        self.assertEqual(po.supplier, self.supplier)
        line = po.lines.get()
        self.assertEqual(line.product, self.prod)
        self.assertEqual(line.ordered_qty, Decimal("10.00"))
        self.assertEqual(line.unit_cost, Decimal("4.00"))

    def test_convert_requires_approved(self):
        from core.models import PurchaseRequisition
        req = self._make_req(status=PurchaseRequisition.Status.DRAFT)
        self.client.post(f"/requisitions/{req.id}/convert/")
        req.refresh_from_db()
        self.assertEqual(req.status, PurchaseRequisition.Status.DRAFT)
        self.assertIsNone(req.converted_po)

    def test_detail_and_list_render(self):
        from core.models import PurchaseRequisition
        req = self._make_req(status=PurchaseRequisition.Status.APPROVED)
        self.assertEqual(self.client.get("/requisitions/").status_code, 200)
        self.assertEqual(self.client.get(f"/requisitions/{req.id}/").status_code, 200)


class InventoryAnalyticsTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        from core.services.inventory import apply_movement
        from django.utils import timezone
        self.tenant = Tenant.objects.create(name="IA Co")
        Location.objects.filter(tenant=self.tenant).delete()  # drop auto-seeded Main Location; this class manages its own
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.loc1 = Location.objects.create(tenant=self.tenant, name="WH1", type=Location.Type.WAREHOUSE)
        self.loc2 = Location.objects.create(tenant=self.tenant, name="WH2", type=Location.Type.WAREHOUSE)
        self.prod = Product.objects.create(tenant=self.tenant, sku="IA1", name="P")
        self.today = timezone.localdate()
        apply_movement(tenant=self.tenant, product=self.prod, location=self.loc1,
                       movement_type="RECEIVE", qty_delta=Decimal("100"), ref_type="S", ref_id="1", unit_cost=Decimal("5.00"))
        apply_movement(tenant=self.tenant, product=self.prod, location=self.loc2,
                       movement_type="RECEIVE", qty_delta=Decimal("20"), ref_type="S", ref_id="2", unit_cost=Decimal("5.00"))
        self.user = User.objects.create_user("iau", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="iau", password="pw")

    def test_valuation_by_location_and_turnover(self):
        from core.services import reports
        from core.services.gl import post_customer_invoice
        from datetime import timedelta
        cust = Customer.objects.create(tenant=self.tenant, name="C")
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=cust, invoice_number="INV-IA")
        CustomerInvoiceLine.objects.create(invoice=inv, product=self.prod, qty=Decimal("10"),
                                           unit_price=Decimal("9"), tax_code=self.std)
        post_customer_invoice(inv)  # COGS 50, stock now 110 @ 5
        data = reports.inventory_analytics(self.tenant, self.today - timedelta(days=1),
                                           self.today + timedelta(days=1))
        self.assertEqual(data["current_value"], Decimal("550.00"))
        self.assertEqual(len(data["by_location"]), 2)
        self.assertEqual(data["cogs"], Decimal("50.00"))
        self.assertIsNotNone(data["turnover"])
        self.assertIsNotNone(data["days_inventory"])

    def test_page_renders(self):
        resp = self.client.get("/reports/inventory-analytics/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Inventory Analytics")


class SupplierScorecardTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        from django.utils import timezone
        self.tenant = Tenant.objects.create(name="Score Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.loc = Location.objects.create(tenant=self.tenant, name="WH", type=Location.Type.WAREHOUSE)
        self.supplier = Supplier.objects.create(tenant=self.tenant, name="Globex")
        self.prod = Product.objects.create(tenant=self.tenant, sku="SC1", name="P")
        self.today = timezone.localdate()
        self.user = User.objects.create_user("scu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="scu", password="pw")

    def test_spend_otd_and_variance(self):
        from core.models import (PurchaseOrder, PurchaseOrderLine, GoodsReceipt,
                                 SupplierInvoice, SupplierInvoiceLine)
        from core.services.gl import post_supplier_invoice
        from core.services import purchasing
        from datetime import timedelta
        po = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-SC", supplier=self.supplier,
                                          status=PurchaseOrder.Status.RECEIVED, expected_date=self.today)
        pol = PurchaseOrderLine.objects.create(po=po, product=self.prod, ordered_qty=Decimal("10"),
                                               unit_cost=Decimal("10.00"), tax_code=self.std)
        grn = GoodsReceipt.objects.create(tenant=self.tenant, po=po, grn_number="GRN-SC", received_to=self.loc,
                                          status=GoodsReceipt.Status.POSTED)
        inv = SupplierInvoice.objects.create(tenant=self.tenant, supplier=self.supplier, po=po,
                                             receipt=grn, invoice_number="BILL-SC", invoice_date=self.today)
        SupplierInvoiceLine.objects.create(invoice=inv, product=self.prod, po_line=pol,
                                           qty=Decimal("10"), unit_cost=Decimal("11.00"), tax_code=self.std)
        post_supplier_invoice(inv)

        data = purchasing.supplier_scorecard(self.tenant, self.today - timedelta(days=1),
                                             self.today + timedelta(days=1))
        r = data["rows"][0]
        self.assertEqual(r["bills"], 1)
        self.assertEqual(r["receipts"], 1)
        self.assertEqual(r["on_time"], 1)
        self.assertEqual(r["otd_pct"], Decimal("100.0"))
        self.assertEqual(r["price_variance"], Decimal("10.00"))  # 10 units x (11-10)
        self.assertEqual(r["spend"], inv.total)

    def test_page_renders(self):
        resp = self.client.get("/reports/supplier-scorecard/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Supplier Scorecard")


class ProfitabilityReportTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        from core.services.inventory import apply_movement
        self.tenant = Tenant.objects.create(name="Profit Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.loc = Location.objects.create(tenant=self.tenant, name="WH", type=Location.Type.WAREHOUSE)
        self.prod = Product.objects.create(tenant=self.tenant, sku="PF1", name="Widget")
        apply_movement(tenant=self.tenant, product=self.prod, location=self.loc,
                       movement_type="RECEIVE", qty_delta=Decimal("100"), ref_type="SEED", ref_id="1",
                       unit_cost=Decimal("6.00"))
        self.cust = Customer.objects.create(tenant=self.tenant, name="Acme")
        self.user = User.objects.create_user("pfu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="pfu", password="pw")

    def _sell(self, qty, price):
        from core.services.gl import post_customer_invoice
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.cust,
                                             invoice_number=f"INV-PF-{qty}-{price}")
        CustomerInvoiceLine.objects.create(invoice=inv, product=self.prod, qty=Decimal(qty),
                                           unit_price=Decimal(price), tax_code=self.std)
        post_customer_invoice(inv)  # deducts stock + posts COGS
        return inv

    def test_margin_by_product_and_customer(self):
        from core.services import sales_reports
        from django.utils import timezone
        from datetime import timedelta
        self._sell("10", "10.00")  # revenue 100, COGS 60, margin 40
        d_from = timezone.localdate() - timedelta(days=1)
        d_to = timezone.localdate() + timedelta(days=1)
        data = sales_reports.profitability(self.tenant, d_from, d_to)
        prow = data["by_product"][0]
        self.assertEqual(prow["revenue"], Decimal("100.00"))
        self.assertEqual(prow["cogs"], Decimal("60.00"))
        self.assertEqual(prow["margin"], Decimal("40.00"))
        self.assertEqual(prow["margin_pct"], Decimal("40.0"))
        crow = data["by_customer"][0]
        self.assertEqual(crow["margin"], Decimal("40.00"))
        self.assertEqual(data["totals"]["margin"], Decimal("40.00"))

    def test_report_page_renders(self):
        self._sell("5", "12.00")
        resp = self.client.get("/sales/reports/profitability/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Profitability")
        self.assertContains(resp, "Widget")


class DunningReminderTests(TestCase):
    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        self.tenant = Tenant.objects.create(name="Dun Co", dunning_enabled=True, dunning_interval_days=7)
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.today = timezone.localdate()
        self.cust = Customer.objects.create(tenant=self.tenant, name="C", email="c@example.com")
        self.cust_noemail = Customer.objects.create(tenant=self.tenant, name="NoMail")
        self.due = self.today - timedelta(days=10)

    def _invoice(self, customer, number, due=None):
        from core.services.gl import post_customer_invoice
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=customer, invoice_number=number,
                                             due_date=(due or self.due))
        CustomerInvoiceLine.objects.create(invoice=inv, description="X", qty=Decimal("1"),
                                           unit_price=Decimal("100"), tax_code=self.std)
        post_customer_invoice(inv)  # -> ISSUED
        return inv

    def test_overdue_reminder_sent_once_per_interval(self):
        from django.core import mail
        from core.services import housekeeping
        inv = self._invoice(self.cust, "INV-D1")
        with self.settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            sent = housekeeping.send_overdue_reminders(self.tenant, today=self.today)
            self.assertEqual(sent, 1)
            self.assertEqual(len(mail.outbox), 1)
            self.assertIn("INV-D1", mail.outbox[0].subject)
            inv.refresh_from_db()
            self.assertEqual(inv.reminder_count, 1)
            self.assertEqual(inv.last_reminder_at, self.today)
            # Same day re-run: throttled, nothing more.
            self.assertEqual(housekeeping.send_overdue_reminders(self.tenant, today=self.today), 0)

    def test_no_email_no_reminder(self):
        from core.services import housekeeping
        self._invoice(self.cust_noemail, "INV-D2")
        with self.settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            self.assertEqual(housekeeping.send_overdue_reminders(self.tenant, today=self.today), 0)

    def test_not_overdue_skipped(self):
        from datetime import timedelta
        from core.services import housekeeping
        self._invoice(self.cust, "INV-D3", due=self.today + timedelta(days=5))
        with self.settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            self.assertEqual(housekeeping.send_overdue_reminders(self.tenant, today=self.today), 0)

    def test_disabled_tenant_sends_nothing(self):
        from core.services import housekeeping
        self.tenant.dunning_enabled = False
        self.tenant.save()
        self._invoice(self.cust, "INV-D4")
        with self.settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            self.assertEqual(housekeeping.send_overdue_reminders(self.tenant, today=self.today), 0)

    def test_paid_invoice_not_reminded(self):
        from core.services import housekeeping
        from core.services.gl import post_payment
        from core.models import Payment, PaymentAllocation
        inv = self._invoice(self.cust, "INV-D5")
        pay = Payment.objects.create(tenant=self.tenant, customer=self.cust,
                                     direction=Payment.Direction.RECEIPT, amount=Decimal("120"),
                                     payment_date=self.today)
        PaymentAllocation.objects.create(payment=pay, customer_invoice=inv, amount=Decimal("120"))
        post_payment(pay)
        with self.settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            self.assertEqual(housekeeping.send_overdue_reminders(self.tenant, today=self.today), 0)


class ReturnToSupplierTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, GLAccount
        from core.services.inventory import apply_movement
        self.tenant = Tenant.objects.create(name="Ret Co")
        self.loc = Location.objects.create(tenant=self.tenant, name="WH", type=Location.Type.WAREHOUSE)
        self.supplier = Supplier.objects.create(tenant=self.tenant, name="Globex")
        self.prod = Product.objects.create(tenant=self.tenant, sku="RT1", name="P")
        apply_movement(tenant=self.tenant, product=self.prod, location=self.loc,
                       movement_type="RECEIVE", qty_delta=Decimal("100"), ref_type="SEED", ref_id="1",
                       unit_cost=Decimal("4.00"))
        self.ap = GLAccount.objects.get(tenant=self.tenant, code="2000")
        self.inv = GLAccount.objects.get(tenant=self.tenant, code="1000")
        self.adj5200 = GLAccount.objects.get(tenant=self.tenant, code="5200")
        self.user = User.objects.create_user("rtu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="rtu", password="pw")

    def _bal(self, acc):
        from core.models import JournalLine
        from django.db.models import Sum
        agg = JournalLine.objects.filter(account=acc).aggregate(d=Sum("debit"), c=Sum("credit"))
        return (agg["d"] or Decimal("0.00")) - (agg["c"] or Decimal("0.00"))

    def test_return_creates_purchase_credit_note(self):
        from core.models import StockAdjustment, CreditNote
        resp = self.client.post("/inventory/adjustments/new/", {
            "product": self.prod.id, "location": self.loc.id, "reason": "RETURN_SUPPLIER",
            "supplier": self.supplier.id, "qty_delta": "-5", "notes": "faulty batch",
        })
        self.assertEqual(resp.status_code, 302)
        adj = StockAdjustment.objects.get(tenant=self.tenant)
        self.assertEqual(adj.status, StockAdjustment.Status.POSTED)
        self.assertIsNotNone(adj.credit_note)
        cn = adj.credit_note
        self.assertEqual(cn.kind, CreditNote.Kind.PURCHASE)
        self.assertEqual(cn.status, CreditNote.Status.POSTED)
        # 5 units @ GBP4 = 20: DR AP (reduces payable) / CR Inventory; no shrinkage.
        self.assertEqual(self._bal(self.ap), Decimal("20.00"))   # debit reduces the credit-normal AP
        self.assertEqual(self._bal(self.inv), Decimal("-20.00"))  # credit reduces inventory
        self.assertEqual(self._bal(self.adj5200), Decimal("0.00"))

    def test_return_requires_supplier(self):
        from core.models import StockAdjustment
        resp = self.client.post("/inventory/adjustments/new/", {
            "product": self.prod.id, "location": self.loc.id, "reason": "RETURN_SUPPLIER",
            "qty_delta": "-5",
        })
        self.assertEqual(resp.status_code, 200)  # re-render with error
        self.assertContains(resp, "Choose the supplier")
        self.assertEqual(StockAdjustment.objects.count(), 0)

    def test_return_must_be_negative(self):
        resp = self.client.post("/inventory/adjustments/new/", {
            "product": self.prod.id, "location": self.loc.id, "reason": "RETURN_SUPPLIER",
            "supplier": self.supplier.id, "qty_delta": "5",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "must remove stock")


class CustomerOrderReservationTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, InventoryBalance, CustomerOrder, CustomerOrderLine, Customer
        self.tenant = Tenant.objects.create(name="Resv Co")
        Location.objects.filter(tenant=self.tenant).delete()  # drop auto-seeded Main Location; this class manages its own
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.loc = Location.objects.create(tenant=self.tenant, name="WH", type=Location.Type.WAREHOUSE)
        self.prod = Product.objects.create(tenant=self.tenant, sku="RV1", name="P")
        InventoryBalance.objects.create(tenant=self.tenant, product=self.prod, location=self.loc,
                                        on_hand=Decimal("50"), reserved=Decimal("0"))
        self.cust = Customer.objects.create(tenant=self.tenant, name="C")
        self.order = CustomerOrder.objects.create(tenant=self.tenant, customer=self.cust, order_number="SO-RV1",
                                                  status=CustomerOrder.Status.DRAFT)
        CustomerOrderLine.objects.create(order=self.order, product=self.prod, qty=Decimal("8"),
                                         unit_price=Decimal("10"), tax_code=self.std)
        self.user = User.objects.create_user("rvu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="rvu", password="pw")

    def _reserved(self):
        from core.models import InventoryBalance
        return InventoryBalance.objects.get(tenant=self.tenant, product=self.prod, location=self.loc).reserved

    def test_confirm_reserves_stock(self):
        from core.models import InventoryReservation
        self.client.post(f"/customer-orders/{self.order.id}/status/confirm/")
        self.assertEqual(self._reserved(), Decimal("8.00"))
        self.assertEqual(InventoryReservation.objects.filter(
            tenant=self.tenant, ref_type="CUSTOMER_ORDER", ref_id=str(self.order.id),
            status=InventoryReservation.Status.ACTIVE).count(), 1)

    def test_cancel_releases_stock(self):
        self.client.post(f"/customer-orders/{self.order.id}/status/confirm/")
        self.assertEqual(self._reserved(), Decimal("8.00"))
        self.client.post(f"/customer-orders/{self.order.id}/status/cancel/")
        self.assertEqual(self._reserved(), Decimal("0.00"))

    def test_invoice_releases_reservation(self):
        self.client.post(f"/customer-orders/{self.order.id}/status/confirm/")
        self.assertEqual(self._reserved(), Decimal("8.00"))
        self.client.post(f"/customer-orders/{self.order.id}/to-invoice/")
        self.assertEqual(self._reserved(), Decimal("0.00"))

    def test_edit_resyncs_reservation(self):
        self.client.post(f"/customer-orders/{self.order.id}/status/confirm/")
        line = self.order.lines.get()
        resp = self.client.post(f"/customer-orders/{self.order.id}/edit/", {
            "customer": self.cust.id, "order_number": "SO-RV1", "order_date": "2026-01-01",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "1",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-id": line.id, "lines-0-product": self.prod.id, "lines-0-qty": "20",
            "lines-0-unit_price": "10", "lines-0-tax_code": self.std.id,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._reserved(), Decimal("20.00"))


class SupplierPriceHistoryTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Price Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.supplier = Supplier.objects.create(tenant=self.tenant, name="Globex")
        self.loc = Location.objects.create(tenant=self.tenant, name="WH", type=Location.Type.WAREHOUSE)
        self.prod = Product.objects.create(tenant=self.tenant, sku="PH1", name="Part")
        self.user = User.objects.create_user("phu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="phu", password="pw")

    def test_record_is_idempotent(self):
        from core.services.purchasing import record_supplier_price
        from core.models import SupplierPriceHistory
        for _ in range(2):
            record_supplier_price(tenant=self.tenant, supplier=self.supplier, product=self.prod,
                                  unit_cost=Decimal("5.00"), source="PO", reference="PO-X")
        self.assertEqual(SupplierPriceHistory.objects.filter(tenant=self.tenant).count(), 1)

    def test_zero_cost_skipped(self):
        from core.services.purchasing import record_supplier_price
        from core.models import SupplierPriceHistory
        record_supplier_price(tenant=self.tenant, supplier=self.supplier, product=self.prod,
                              unit_cost=Decimal("0.00"), source="PO", reference="PO-Z")
        self.assertEqual(SupplierPriceHistory.objects.count(), 0)

    def test_po_submit_records_price(self):
        from core.models import PurchaseOrder, PurchaseOrderLine, SupplierPriceHistory
        po = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-PH", supplier=self.supplier,
                                          status=PurchaseOrder.Status.DRAFT)
        PurchaseOrderLine.objects.create(po=po, product=self.prod, ordered_qty=Decimal("10"),
                                         unit_cost=Decimal("7.50"), tax_code=self.std)
        self.client.post(f"/po/{po.id}/submit/")
        rec = SupplierPriceHistory.objects.get(tenant=self.tenant, supplier=self.supplier, product=self.prod, source="PO")
        self.assertEqual(rec.unit_cost, Decimal("7.50"))

    def test_bill_posting_records_actual_price(self):
        from core.models import (PurchaseOrder, GoodsReceipt, SupplierInvoice, SupplierInvoiceLine,
                                 SupplierPriceHistory)
        from core.services.gl import post_supplier_invoice
        po = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-B", supplier=self.supplier,
                                          status=PurchaseOrder.Status.RECEIVED)
        grn = GoodsReceipt.objects.create(tenant=self.tenant, po=po, grn_number="GRN-B", received_to=self.loc,
                                          status=GoodsReceipt.Status.POSTED)
        inv = SupplierInvoice.objects.create(tenant=self.tenant, supplier=self.supplier, po=po, receipt=grn,
                                             invoice_number="BILL-B")
        SupplierInvoiceLine.objects.create(invoice=inv, product=self.prod, qty=Decimal("10"),
                                           unit_cost=Decimal("8.25"), tax_code=self.std)
        post_supplier_invoice(inv)
        rec = SupplierPriceHistory.objects.get(tenant=self.tenant, supplier=self.supplier, product=self.prod, source="BILL")
        self.assertEqual(rec.unit_cost, Decimal("8.25"))

    def test_last_prices_and_json_endpoint(self):
        from core.services.purchasing import record_supplier_price, last_prices_for_supplier
        from django.utils import timezone
        from datetime import timedelta
        record_supplier_price(tenant=self.tenant, supplier=self.supplier, product=self.prod,
                              unit_cost=Decimal("5.00"), source="PO", reference="PO-1",
                              recorded_at=timezone.localdate() - timedelta(days=10))
        record_supplier_price(tenant=self.tenant, supplier=self.supplier, product=self.prod,
                              unit_cost=Decimal("6.00"), source="BILL", reference="BILL-1",
                              recorded_at=timezone.localdate())
        last = last_prices_for_supplier(self.tenant, self.supplier)
        self.assertEqual(last[self.prod.id], Decimal("6.00"))  # most recent
        resp = self.client.get(f"/po/supplier/{self.supplier.id}/prices/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["prices"][str(self.prod.id)], "6.00")


class StockAdjustmentGLTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, GLAccount
        from core.services.inventory import apply_movement
        self.tenant = Tenant.objects.create(name="Adj Co")
        self.loc = Location.objects.create(tenant=self.tenant, name="WH", type=Location.Type.WAREHOUSE)
        self.prod = Product.objects.create(tenant=self.tenant, sku="ADJ1", name="P")
        # Seed 100 units @ £2 so the product has an average cost.
        apply_movement(tenant=self.tenant, product=self.prod, location=self.loc,
                       movement_type="RECEIVE", qty_delta=Decimal("100"), ref_type="SEED", ref_id="1",
                       unit_cost=Decimal("2.00"))
        self.inv_acc = GLAccount.objects.get(tenant=self.tenant, code="1000")
        self.adj_acc = GLAccount.objects.get(tenant=self.tenant, code="5200")
        self.user = User.objects.create_user("adju", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="adju", password="pw")

    def _acc_balance(self, acc):
        from core.models import JournalLine
        from django.db.models import Sum
        agg = JournalLine.objects.filter(account=acc).aggregate(d=Sum("debit"), c=Sum("credit"))
        return (agg["d"] or Decimal("0.00")) - (agg["c"] or Decimal("0.00"))

    def test_write_off_posts_loss_to_gl(self):
        from core.models import StockAdjustment, JournalEntry
        resp = self.client.post("/inventory/adjustments/new/", {
            "product": self.prod.id, "location": self.loc.id, "reason": "WRITE_OFF",
            "qty_delta": "-10", "notes": "broken",
        })
        self.assertEqual(resp.status_code, 302)
        adj = StockAdjustment.objects.get(tenant=self.tenant)
        self.assertEqual(adj.status, StockAdjustment.Status.POSTED)
        JournalEntry.objects.get(tenant=self.tenant, ref_type="STOCK_ADJ", ref_id=str(adj.id))
        # 10 units @ £2 = £20 loss: DR 5200 / CR 1000 (only GL postings hit acct 1000)
        self.assertEqual(self._acc_balance(self.adj_acc), Decimal("20.00"))
        self.assertEqual(self._acc_balance(self.inv_acc), Decimal("-20.00"))

    def test_found_stock_posts_gain_to_gl(self):
        from core.models import StockAdjustment
        self.client.post("/inventory/adjustments/new/", {
            "product": self.prod.id, "location": self.loc.id, "reason": "ADJUSTMENT",
            "qty_delta": "5", "notes": "found",
        })
        adj = StockAdjustment.objects.get(tenant=self.tenant)
        self.assertEqual(adj.status, StockAdjustment.Status.POSTED)
        # 5 units @ £2 = £10 gain: DR 1000 / CR 5200
        self.assertEqual(self._acc_balance(self.adj_acc), Decimal("-10.00"))
        self.assertEqual(self._acc_balance(self.inv_acc), Decimal("10.00"))

    def test_je_balances(self):
        from core.models import StockAdjustment, JournalEntry
        from django.db.models import Sum
        self.client.post("/inventory/adjustments/new/", {
            "product": self.prod.id, "location": self.loc.id, "reason": "DAMAGE",
            "qty_delta": "-3", "notes": "",
        })
        adj = StockAdjustment.objects.get(tenant=self.tenant)
        je = JournalEntry.objects.get(tenant=self.tenant, ref_type="STOCK_ADJ", ref_id=str(adj.id))
        agg = je.lines.aggregate(d=Sum("debit"), c=Sum("credit"))
        self.assertEqual(agg["d"], agg["c"])


class CreditLimitEnforcementTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Credit Co")
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.user = User.objects.create_user("credu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="credu", password="pw")

    def _invoice(self, customer, net):
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=customer,
                                             invoice_number=f"INV-{customer.id}-{net}")
        CustomerInvoiceLine.objects.create(invoice=inv, description="X", qty=Decimal("1"),
                                           unit_price=Decimal(net), tax_code=self.std)
        return inv

    def test_credit_status_method(self):
        c = Customer.objects.create(tenant=self.tenant, name="A", credit_limit=Decimal("500.00"))
        ok, _ = c.credit_status(Decimal("100.00"))
        self.assertTrue(ok)
        ok, reason = c.credit_status(Decimal("600.00"))
        self.assertFalse(ok)
        self.assertIn("Credit limit exceeded", reason)

    def test_no_limit_allows_anything(self):
        c = Customer.objects.create(tenant=self.tenant, name="B", credit_limit=Decimal("0.00"))
        ok, _ = c.credit_status(Decimal("999999.00"))
        self.assertTrue(ok)

    def test_on_hold_blocks(self):
        c = Customer.objects.create(tenant=self.tenant, name="C", status=Customer.Status.ON_HOLD)
        ok, reason = c.credit_status(Decimal("1.00"))
        self.assertFalse(ok)
        self.assertIn("on hold", reason)

    def test_issue_blocked_when_over_limit(self):
        c = Customer.objects.create(tenant=self.tenant, name="D", credit_limit=Decimal("100.00"))
        inv = self._invoice(c, "200")  # 240 gross > 100 limit
        resp = self.client.post(f"/ar/invoices/{inv.id}/issue/")
        self.assertEqual(resp.status_code, 302)
        inv.refresh_from_db()
        self.assertEqual(inv.status, CustomerInvoice.Status.DRAFT)  # not issued

    def test_issue_succeeds_when_within_limit(self):
        c = Customer.objects.create(tenant=self.tenant, name="E", credit_limit=Decimal("1000.00"))
        inv = self._invoice(c, "200")  # 240 gross < 1000
        resp = self.client.post(f"/ar/invoices/{inv.id}/issue/")
        self.assertEqual(resp.status_code, 302)
        inv.refresh_from_db()
        self.assertEqual(inv.status, CustomerInvoice.Status.ISSUED)


class LowStockReorderTests(TestCase):
    def setUp(self):
        from core.models import OrgMembership, InventoryBalance
        self.tenant = Tenant.objects.create(name="Reorder Co")
        self.loc = Location.objects.create(tenant=self.tenant, name="WH", type=Location.Type.WAREHOUSE)
        self.supA = Supplier.objects.create(tenant=self.tenant, name="Supplier A")
        self.supB = Supplier.objects.create(tenant=self.tenant, name="Supplier B")
        # Two products below reorder for supplier A, one for B, one unassigned.
        self.p1 = Product.objects.create(tenant=self.tenant, sku="R1", name="P1", reorder_level=Decimal("20"),
                                         preferred_supplier=self.supA, standard_cost=Decimal("2.00"))
        self.p2 = Product.objects.create(tenant=self.tenant, sku="R2", name="P2", reorder_level=Decimal("30"),
                                         preferred_supplier=self.supA, standard_cost=Decimal("5.00"))
        self.p3 = Product.objects.create(tenant=self.tenant, sku="R3", name="P3", reorder_level=Decimal("10"),
                                         preferred_supplier=self.supB)
        self.p4 = Product.objects.create(tenant=self.tenant, sku="R4", name="P4", reorder_level=Decimal("10"))
        for p, oh in [(self.p1, "5"), (self.p2, "0"), (self.p3, "2"), (self.p4, "1")]:
            InventoryBalance.objects.create(tenant=self.tenant, product=p, location=self.loc, on_hand=Decimal(oh))
        self.user = User.objects.create_user("ro", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="ro", password="pw")

    def test_low_stock_lists_shortfalls(self):
        resp = self.client.get("/inventory/low-stock/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["rows"]), 4)

    def test_reorder_groups_by_supplier(self):
        from core.models import PurchaseRequisition
        resp = self.client.post("/inventory/low-stock/reorder/", {
            "select": [str(self.p1.id), str(self.p2.id), str(self.p3.id), str(self.p4.id)],
            f"qty_{self.p1.id}": "15", f"qty_{self.p2.id}": "30",
            f"qty_{self.p3.id}": "8", f"qty_{self.p4.id}": "9",
        })
        self.assertEqual(resp.status_code, 302)
        # 3 groups: supA (2 lines), supB (1), unassigned (1)
        reqs = PurchaseRequisition.objects.filter(tenant=self.tenant)
        self.assertEqual(reqs.count(), 3)
        a = reqs.get(preferred_supplier=self.supA)
        self.assertEqual(a.lines.count(), 2)
        self.assertEqual(a.status, PurchaseRequisition.Status.DRAFT)
        line1 = a.lines.get(product=self.p1)
        self.assertEqual(line1.quantity, Decimal("15.00"))
        self.assertEqual(line1.estimated_unit_cost, Decimal("2.00"))
        self.assertEqual(reqs.get(preferred_supplier=self.supB).lines.count(), 1)
        self.assertEqual(reqs.filter(preferred_supplier__isnull=True).count(), 1)

    def test_reorder_skips_zero_qty(self):
        from core.models import PurchaseRequisition
        self.client.post("/inventory/low-stock/reorder/", {
            "select": [str(self.p1.id)], f"qty_{self.p1.id}": "0",
        })
        self.assertEqual(PurchaseRequisition.objects.filter(tenant=self.tenant).count(), 0)

    def test_reorder_requires_selection(self):
        from core.models import PurchaseRequisition
        self.client.post("/inventory/low-stock/reorder/", {})
        self.assertEqual(PurchaseRequisition.objects.filter(tenant=self.tenant).count(), 0)


class DefaultLocationTests(TestCase):
    """A fresh organisation is provisioned with a single 'Main Location'."""

    def test_new_tenant_gets_main_location(self):
        from core.models import Location
        t = Tenant.objects.create(name="Fresh Co")
        locs = Location.objects.filter(tenant=t)
        self.assertEqual(locs.count(), 1)
        loc = locs.get()
        self.assertEqual(loc.name, "Main Location")
        self.assertEqual(loc.type, Location.Type.WAREHOUSE)
        self.assertTrue(loc.holds_stock)
        self.assertTrue(loc.is_active)

    def test_seed_is_idempotent_on_resave(self):
        from core.models import Location
        t = Tenant.objects.create(name="Resave Co")
        t.name = "Resave Co Ltd"
        t.save()
        self.assertEqual(Location.objects.filter(tenant=t).count(), 1)

    def test_does_not_seed_when_locations_exist(self):
        # Simulate a tenant that already had a location before the save signal
        # re-fires: no duplicate Main Location is added.
        from core.models import Location
        t = Tenant.objects.create(name="Has Loc Co")
        Location.objects.filter(tenant=t).delete()
        Location.objects.create(tenant=t, name="Depot", type=Location.Type.WAREHOUSE)
        t.save()
        names = set(Location.objects.filter(tenant=t).values_list("name", flat=True))
        self.assertEqual(names, {"Depot"})

    def test_new_organisation_view_provisions_location(self):
        from core.models import Location, OrgMembership
        u = User.objects.create_user("orgcreator", password="pw")
        self.client.login(username="orgcreator", password="pw")
        resp = self.client.post("/onboarding/new-organisation/", {
            "name": "View Co", "currency_code": "GBP", "country": "United Kingdom",
        })
        self.assertEqual(resp.status_code, 302)
        t = Tenant.objects.get(name="View Co")
        self.assertEqual(Location.objects.filter(tenant=t, name="Main Location").count(), 1)


class LocationAccessEnforcementTests(TestCase):
    """A location-restricted user only sees/acts on their granted locations in
    stock adjustments, transfers and goods receipts."""

    def setUp(self):
        from core.models import (OrgMembership, UserLocationAccess, StockAdjustment,
                                 InventoryTransfer, InventoryBalance, Site)
        self.tenant = Tenant.objects.create(name="Acc Co")
        Location.objects.filter(tenant=self.tenant).delete()
        # locA under the default Main Site; locB under a second site.
        self.siteB = Site.objects.create(tenant=self.tenant, name="Site B", site_type=Site.Type.CITY_BRANCH)
        self.locA = Location.objects.create(tenant=self.tenant, name="WH-A", type=Location.Type.WAREHOUSE)
        self.locB = Location.objects.create(tenant=self.tenant, site=self.siteB, name="WH-B", type=Location.Type.WAREHOUSE)
        self.prod = Product.objects.create(tenant=self.tenant, sku="ACC1", name="P")
        # Warehouse user restricted to WH-A only.
        self.user = User.objects.create_user("whA", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="WAREHOUSE", is_default=True)
        UserLocationAccess.objects.create(tenant=self.tenant, user=self.user, location=self.locA)
        self.client.login(username="whA", password="pw")
        # One adjustment at each location.
        self.adjA = StockAdjustment.objects.create(tenant=self.tenant, product=self.prod, location=self.locA,
                                                   qty_delta=Decimal("1"), requested_by=self.user)
        self.adjB = StockAdjustment.objects.create(tenant=self.tenant, product=self.prod, location=self.locB,
                                                   qty_delta=Decimal("1"), requested_by=self.user)
        # A transfer entirely within WH-B (not accessible to whA).
        self.trB = InventoryTransfer.objects.create(tenant=self.tenant, transfer_number="TR-B",
                                                    from_location=self.locB, to_location=self.locB)

    def test_adjustment_list_filtered_to_accessible(self):
        resp = self.client.get("/inventory/adjustments/")
        ids = [a.id for a in resp.context["adjustments"]]
        self.assertIn(self.adjA.id, ids)
        self.assertNotIn(self.adjB.id, ids)

    def test_transfer_list_filtered_to_accessible(self):
        from core.models import InventoryTransfer
        trA = InventoryTransfer.objects.create(tenant=self.tenant, transfer_number="TR-A",
                                               from_location=self.locA, to_location=self.locB)
        resp = self.client.get("/transfers/")
        ids = [t.id for t in resp.context["transfers"]]
        self.assertIn(trA.id, ids)        # touches WH-A
        self.assertNotIn(self.trB.id, ids)  # entirely in WH-B

    def test_transfer_post_blocked_for_inaccessible_locations(self):
        resp = self.client.post(f"/transfers/{self.trB.id}/post/")
        self.assertEqual(resp.status_code, 403)

    def test_admin_also_scoped_to_selected_site(self):
        # Even an admin sees only the selected site - there is no combined view.
        from core.models import OrgMembership
        admin = User.objects.create_user("accadmin", password="pw")
        OrgMembership.objects.create(user=admin, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="accadmin", password="pw")  # auto-selects Main Site (first)
        ids = [a.id for a in self.client.get("/inventory/adjustments/").context["adjustments"]]
        self.assertIn(self.adjA.id, ids)
        self.assertNotIn(self.adjB.id, ids)  # adjB is at a location under another site
        # Switching site reveals the other site's data (and hides the first).
        self.client.post("/switch-site/", {"site": self.siteB.id})
        ids = [a.id for a in self.client.get("/inventory/adjustments/").context["adjustments"]]
        self.assertIn(self.adjB.id, ids)
        self.assertNotIn(self.adjA.id, ids)

    def test_can_access_location_helper(self):
        from core.views import _can_access_location

        class _Req:
            user = self.user
        self.assertTrue(_can_access_location(_Req(), self.tenant, self.locA.id))
        self.assertFalse(_can_access_location(_Req(), self.tenant, self.locB.id))
        self.assertFalse(_can_access_location(_Req(), self.tenant, self.locA.id, self.locB.id))
        self.assertTrue(_can_access_location(_Req(), self.tenant, None))  # None ignored


class DepartmentTests(TestCase):
    """Department/Team tier: model, CRUD pages, membership link, manager scoping."""

    def setUp(self):
        from core.models import OrgMembership
        self.tenant = Tenant.objects.create(name="Dept Co")
        self.other = Tenant.objects.create(name="Other Co")
        self.admin = User.objects.create_user("deptadmin", password="pw")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)
        self.member = User.objects.create_user("deptmember", password="pw")
        OrgMembership.objects.create(user=self.member, tenant=self.tenant, role="WAREHOUSE")
        # A user in another org only - must not appear as a manager option here.
        self.outsider = User.objects.create_user("outsider", password="pw")
        OrgMembership.objects.create(user=self.outsider, tenant=self.other, role="ADMIN")
        self.client.login(username="deptadmin", password="pw")

    def test_create_department_via_view(self):
        from core.models import Department
        resp = self.client.post("/departments/new/", {
            "name": "Warehouse Ops", "code": "WHO", "manager": self.member.id, "is_active": "on",
        })
        self.assertEqual(resp.status_code, 302)
        d = Department.objects.get(tenant=self.tenant, name="Warehouse Ops")
        self.assertEqual(d.code, "WHO")
        self.assertEqual(d.manager, self.member)
        self.assertTrue(d.is_active)

    def test_manager_choices_limited_to_org_members(self):
        from core.forms import DepartmentForm
        from core.current import set_current_tenant, clear_current_tenant
        set_current_tenant(self.tenant)
        try:
            ids = set(DepartmentForm().fields["manager"].queryset.values_list("id", flat=True))
        finally:
            clear_current_tenant()
        self.assertIn(self.admin.id, ids)
        self.assertIn(self.member.id, ids)
        self.assertNotIn(self.outsider.id, ids)

    def test_membership_department_link(self):
        from core.models import Department, OrgMembership
        d = Department.objects.create(tenant=self.tenant, name="Sales")
        m = OrgMembership.objects.get(user=self.member, tenant=self.tenant)
        m.department = d
        m.save()
        self.assertEqual(d.members.count(), 1)
        self.assertEqual(d.members.get(), m)

    def test_delete_department_unassigns_members(self):
        from core.models import Department, OrgMembership
        d = Department.objects.create(tenant=self.tenant, name="Temp")
        m = OrgMembership.objects.get(user=self.member, tenant=self.tenant)
        m.department = d
        m.save()
        resp = self.client.post(f"/departments/{d.id}/delete/")
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Department.objects.filter(id=d.id).exists())
        m.refresh_from_db()
        self.assertIsNone(m.department_id)  # SET_NULL

    def test_duplicate_name_rejected(self):
        from core.models import Department
        Department.objects.create(tenant=self.tenant, name="Finance")
        resp = self.client.post("/departments/new/", {"name": "Finance", "is_active": "on"})
        self.assertEqual(resp.status_code, 200)  # re-rendered with error
        self.assertEqual(Department.objects.filter(tenant=self.tenant, name="Finance").count(), 1)

    def test_list_page_renders(self):
        from core.models import Department
        Department.objects.create(tenant=self.tenant, name="Ops")
        resp = self.client.get("/departments/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Ops")

    def test_non_admin_cannot_create(self):
        self.client.login(username="deptmember", password="pw")
        resp = self.client.post("/departments/new/", {"name": "Sneaky", "is_active": "on"})
        self.assertEqual(resp.status_code, 403)


class SalesLocationTests(TestCase):
    """Sales documents carry a location; fulfilment and reports honour it."""

    def setUp(self):
        from core.models import OrgMembership, InventoryMovement
        from core.services.inventory import apply_movement
        self.tenant = Tenant.objects.create(name="SalesLoc Co")
        Location.objects.filter(tenant=self.tenant).delete()
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.locA = Location.objects.create(tenant=self.tenant, name="Shop A", type=Location.Type.SHOP)
        self.locB = Location.objects.create(tenant=self.tenant, name="Shop B", type=Location.Type.SHOP)
        self.prod = Product.objects.create(tenant=self.tenant, sku="SL1", name="Widget",
                                           cost_method=Product.CostMethod.AVERAGE)
        for loc in (self.locA, self.locB):
            apply_movement(tenant=self.tenant, product=self.prod, location=loc,
                           movement_type=InventoryMovement.MovementType.RECEIVE, qty_delta=Decimal("100"),
                           ref_type="SEED", ref_id=f"OPEN-{loc.id}", unit_cost=Decimal("4.00"))
        self.customer = Customer.objects.create(tenant=self.tenant, name="Cust")
        self.user = User.objects.create_user("sluser", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="sluser", password="pw")

    def test_invoice_fulfils_from_its_location(self):
        from core.models import InventoryBalance
        from core.services.gl import post_customer_invoice
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer,
                                             invoice_number="INV-SL1", location=self.locB)
        CustomerInvoiceLine.objects.create(invoice=inv, product=self.prod, qty=Decimal("10"),
                                           unit_price=Decimal("25.00"), tax_code=self.std)
        post_customer_invoice(inv)
        balA = InventoryBalance.objects.get(tenant=self.tenant, product=self.prod, location=self.locA)
        balB = InventoryBalance.objects.get(tenant=self.tenant, product=self.prod, location=self.locB)
        self.assertEqual(balA.on_hand, Decimal("100.00"))  # untouched
        self.assertEqual(balB.on_hand, Decimal("90.00"))   # deducted here

    def test_invoice_without_location_falls_back(self):
        from core.models import InventoryBalance
        from core.services.gl import post_customer_invoice
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer,
                                             invoice_number="INV-SL2")  # no location
        CustomerInvoiceLine.objects.create(invoice=inv, product=self.prod, qty=Decimal("5"),
                                           unit_price=Decimal("25.00"), tax_code=self.std)
        post_customer_invoice(inv)
        # Falls back to first stock location (locA by id) - still deducts somewhere.
        total = sum(b.on_hand for b in InventoryBalance.objects.filter(tenant=self.tenant, product=self.prod))
        self.assertEqual(total, Decimal("195.00"))  # 200 - 5

    def test_create_view_defaults_location(self):
        resp = self.client.post("/ar/invoices/new/", {
            "customer": self.customer.id, "invoice_date": "2026-01-01", "action": "save",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": self.prod.id, "lines-0-qty": "1", "lines-0-unit_price": "10",
            "lines-0-tax_code": self.std.id,
        })
        self.assertEqual(resp.status_code, 302)
        inv = CustomerInvoice.objects.get(tenant=self.tenant, customer=self.customer)
        self.assertIsNotNone(inv.location_id)  # defaulted to a stock location

    def test_order_to_invoice_carries_location(self):
        from core.models import CustomerOrder, CustomerOrderLine
        o = CustomerOrder.objects.create(tenant=self.tenant, customer=self.customer,
                                         order_number="SO-SL1", location=self.locB,
                                         status=CustomerOrder.Status.CONFIRMED)
        CustomerOrderLine.objects.create(order=o, product=self.prod, qty=Decimal("3"),
                                         unit_price=Decimal("10"), tax_code=self.std)
        resp = self.client.post(f"/customer-orders/{o.id}/to-invoice/")
        self.assertEqual(resp.status_code, 302)
        inv = CustomerInvoice.objects.get(source_order=o)
        self.assertEqual(inv.location_id, self.locB.id)

    def test_sales_history_location_filter(self):
        from core.services import sales_reports
        from datetime import date
        for n, loc in (("INV-A", self.locA), ("INV-B", self.locB)):
            inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.customer,
                                                 invoice_number=n, location=loc,
                                                 status=CustomerInvoice.Status.ISSUED,
                                                 invoice_date=date(2026, 1, 15))
            CustomerInvoiceLine.objects.create(invoice=inv, product=self.prod, qty=Decimal("1"),
                                               unit_price=Decimal("50"), tax_code=self.std)
        df, dt = date(2026, 1, 1), date(2026, 12, 31)
        all_rows = sales_reports.sales_history(self.tenant, df, dt)["rows"]
        b_rows = sales_reports.sales_history(self.tenant, df, dt, location_ids=[self.locB.id])["rows"]
        self.assertEqual(len(all_rows), 2)
        self.assertEqual(len(b_rows), 1)
        self.assertEqual(b_rows[0]["invoice"].invoice_number, "INV-B")


class POReceivingLocationTests(TestCase):
    """Purchase orders carry a structured receiving location; shipments and
    goods receipts land stock there."""

    def setUp(self):
        from core.models import OrgMembership, Supplier
        self.tenant = Tenant.objects.create(name="POLoc Co")
        Location.objects.filter(tenant=self.tenant).delete()
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.locA = Location.objects.create(tenant=self.tenant, name="WH A", type=Location.Type.WAREHOUSE)
        self.locB = Location.objects.create(tenant=self.tenant, name="WH B", type=Location.Type.WAREHOUSE)
        self.supplier = Supplier.objects.create(tenant=self.tenant, name="Supp")
        self.prod = Product.objects.create(tenant=self.tenant, sku="POL1", name="Widget")
        self.user = User.objects.create_user("poluser", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="poluser", password="pw")

    def _post_po(self, receiving_location=None, action="save"):
        data = {
            "supplier": self.supplier.id, "expected_date": "2026-02-01", "action": action,
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": self.prod.id, "lines-0-ordered_qty": "10",
            "lines-0-unit_cost": "4.00", "lines-0-tax_code": self.std.id,
        }
        if receiving_location is not None:
            data["receiving_location"] = receiving_location.id
        return self.client.post("/po/new/", data)

    def test_po_defaults_receiving_location(self):
        from core.models import PurchaseOrder
        resp = self._post_po()
        self.assertEqual(resp.status_code, 302)
        po = PurchaseOrder.objects.get(tenant=self.tenant)
        self.assertIsNotNone(po.receiving_location_id)  # defaulted to a stock location

    def test_explicit_receiving_location_drives_shipment_destination(self):
        from core.models import PurchaseOrder, Shipment
        resp = self._post_po(receiving_location=self.locB, action="submit")
        self.assertEqual(resp.status_code, 302)
        po = PurchaseOrder.objects.get(tenant=self.tenant)
        self.assertEqual(po.receiving_location_id, self.locB.id)
        shipment = Shipment.objects.get(po=po)
        self.assertEqual(shipment.destination_id, self.locB.id)

    def test_po_destination_helper(self):
        from core.models import PurchaseOrder
        from core.views import _po_destination
        po = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-H1",
                                          supplier=self.supplier, receiving_location=self.locB)
        self.assertEqual(_po_destination(po), self.locB)
        po2 = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-H2", supplier=self.supplier)
        self.assertEqual(_po_destination(po2), self.locA)  # fallback to first location


class RequisitionDepartmentFKTests(TestCase):
    """PurchaseRequisition.department is a structured Department FK; access-request
    approval can assign a department to the new member."""

    def setUp(self):
        from core.models import OrgMembership, Department
        self.tenant = Tenant.objects.create(name="ReqDept Co")
        self.other = Tenant.objects.create(name="ReqDept Other")
        self.dept = Department.objects.create(tenant=self.tenant, name="Procurement")
        self.foreign_dept = Department.objects.create(tenant=self.other, name="Foreign")
        self.admin = User.objects.create_user("rdadmin", password="pw")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="rdadmin", password="pw")

    def test_requisition_form_scopes_department_to_tenant(self):
        from core.forms import PurchaseRequisitionForm
        from core.current import set_current_tenant, clear_current_tenant
        set_current_tenant(self.tenant)
        try:
            ids = set(PurchaseRequisitionForm().fields["department"].queryset.values_list("id", flat=True))
        finally:
            clear_current_tenant()
        self.assertIn(self.dept.id, ids)
        self.assertNotIn(self.foreign_dept.id, ids)  # another org's dept excluded

    def test_requisition_links_department(self):
        from core.models import PurchaseRequisition
        req = PurchaseRequisition.objects.create(tenant=self.tenant, req_number="PR-D1", department=self.dept)
        self.assertEqual(req.department, self.dept)
        self.assertEqual(self.dept.requisitions.get(), req)

    def test_deleting_department_nulls_requisition(self):
        from core.models import PurchaseRequisition, Department
        d = Department.objects.create(tenant=self.tenant, name="Temp")
        req = PurchaseRequisition.objects.create(tenant=self.tenant, req_number="PR-D2", department=d)
        d.delete()
        req.refresh_from_db()
        self.assertIsNone(req.department_id)  # SET_NULL

    def test_access_request_approval_assigns_department(self):
        from core.models import AccessRequest, OrgMembership
        ar = AccessRequest.objects.create(name="Jane Doe", email="jane@x.example", team="Procurement")
        resp = self.client.post(f"/access-requests/{ar.id}/action/", {
            "action": "approve", "role": "PURCHASING", "department": self.dept.id,
        })
        self.assertEqual(resp.status_code, 302)
        ar.refresh_from_db()
        m = OrgMembership.objects.get(user=ar.created_user, tenant=self.tenant)
        self.assertEqual(m.department, self.dept)

    def test_access_request_approval_without_department(self):
        from core.models import AccessRequest, OrgMembership
        ar = AccessRequest.objects.create(name="No Dept", email="nd@x.example", team="")
        resp = self.client.post(f"/access-requests/{ar.id}/action/", {
            "action": "approve", "role": "SALES",
        })
        self.assertEqual(resp.status_code, 302)
        ar.refresh_from_db()
        m = OrgMembership.objects.get(user=ar.created_user, tenant=self.tenant)
        self.assertIsNone(m.department_id)


class SiteContextTests(TestCase):
    """Mandatory company + site context: auto-select, gate, switching, 403, audit.
    There is never an 'all sites' option."""
    client_class = Client  # exercise the real gate, not the auto-context client

    def setUp(self):
        from core.models import OrgMembership, Site
        self.tenant = Tenant.objects.create(name="Ctx Co")
        self.main_site = Site.objects.get(tenant=self.tenant, is_default=True)
        self.user = User.objects.create_user("ctxu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)

    def _login(self):
        self.client.login(username="ctxu", password="pw")

    def test_single_company_single_site_autoselects(self):
        self._login()
        resp = self.client.get("/inventory/")
        self.assertEqual(resp.status_code, 200)  # no redirect - auto-selected
        self.assertEqual(self.client.session["active_tenant_id"], self.tenant.id)
        self.assertEqual(self.client.session["active_site_id"], self.main_site.id)

    def test_multiple_sites_force_selection(self):
        from core.models import AuditLog, Site
        siteB = Site.objects.create(tenant=self.tenant, name="Leicester", site_type=Site.Type.CITY_BRANCH)
        self._login()
        resp = self.client.get("/inventory/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/select-site/", resp["Location"])
        # Pick a specific site -> proceeds, context set, audited.
        resp2 = self.client.post("/select-site/", {"site": siteB.id, "next": "/inventory/"})
        self.assertRedirects(resp2, "/inventory/")
        self.assertEqual(self.client.session["active_site_id"], siteB.id)
        self.assertTrue(AuditLog.objects.filter(tenant=self.tenant, action="SITE_SELECTED").exists())

    def test_site_picker_has_no_all_sites_option(self):
        from core.models import Site
        Site.objects.create(tenant=self.tenant, name="Leicester", site_type=Site.Type.CITY_BRANCH)
        self._login()
        resp = self.client.get("/select-site/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "All sites")
        self.assertNotContains(resp, "All Sites")

    def test_multiple_companies_force_company_then_site(self):
        from core.models import OrgMembership, Tenant as T
        t2 = T.objects.create(name="Ctx Co 2")  # gets its own default Site via signal
        OrgMembership.objects.create(user=self.user, tenant=t2, role="ADMIN")
        self._login()
        resp = self.client.get("/inventory/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/select-org/", resp["Location"])
        # Choose company -> then site auto-resolves (each has one) -> dashboard reachable.
        self.client.post("/select-org/", {"tenant": self.tenant.id})
        self.assertEqual(self.client.session["active_tenant_id"], self.tenant.id)

    def test_switch_company_clears_site(self):
        from core.models import OrgMembership, Tenant as T, AuditLog
        t2 = T.objects.create(name="Ctx Co 2")
        OrgMembership.objects.create(user=self.user, tenant=t2, role="ADMIN")
        self._login()
        s = self.client.session
        s["active_tenant_id"] = self.tenant.id
        s["active_site_id"] = self.main_site.id
        s.save()
        resp = self.client.post("/switch-company/", {"tenant": t2.id})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.session["active_tenant_id"], t2.id)
        self.assertNotIn("active_site_id", self.client.session)  # site cleared
        self.assertTrue(AuditLog.objects.filter(action="COMPANY_SWITCHED").exists())

    def test_switch_site_stays_on_page(self):
        from core.models import AuditLog, Site
        siteB = Site.objects.create(tenant=self.tenant, name="Leicester", site_type=Site.Type.CITY_BRANCH)
        self._login()
        self.client.get("/inventory/")  # auto-selects... multiple sites now -> may redirect; set context
        s = self.client.session
        s["active_site_id"] = self.main_site.id
        s.save()
        resp = self.client.post("/switch-site/", {"site": siteB.id, "next": "/inventory/"})
        self.assertRedirects(resp, "/inventory/")
        self.assertEqual(self.client.session["active_site_id"], siteB.id)
        self.assertTrue(AuditLog.objects.filter(action="SITE_SWITCHED").exists())

    def test_unauthorised_site_returns_403_and_audits(self):
        from core.models import Tenant as T, Site, AuditLog
        other = T.objects.create(name="Other Ctx")
        other_site = Site.objects.filter(tenant=other).first()
        self._login()
        s = self.client.session
        s["active_tenant_id"] = self.tenant.id
        s["active_site_id"] = self.main_site.id
        s.save()
        resp = self.client.post("/switch-site/", {"site": other_site.id})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(AuditLog.objects.filter(action="UNAUTHORISED_SITE_ACCESS").exists())

    def test_unauthorised_company_returns_403(self):
        from core.models import Tenant as T
        other = T.objects.create(name="Other Ctx 2")  # user is not a member
        self._login()
        resp = self.client.post("/switch-company/", {"tenant": other.id})
        self.assertEqual(resp.status_code, 403)

    def test_restricted_user_no_site_shows_no_site_page(self):
        from core.models import OrgMembership, UserSiteAccess
        # A warehouse user granted only an inactive site -> no selectable site.
        self.main_site.is_active = False
        self.main_site.save()
        u2 = User.objects.create_user("ctxnos", password="pw")
        OrgMembership.objects.create(user=u2, tenant=self.tenant, role="WAREHOUSE", is_default=True)
        UserSiteAccess.objects.create(tenant=self.tenant, user=u2, site=self.main_site)
        self.client.login(username="ctxnos", password="pw")
        resp = self.client.get("/inventory/", follow=True)
        self.assertContains(resp, "No site available")


class WorkspaceSwitcherTests(TestCase):
    """The two-pane workspace picker switches company + site atomically via
    /switch-workspace/, validating access to both before applying either."""
    client_class = Client

    def setUp(self):
        from core.models import OrgMembership, Tenant as T, Site
        self.t1 = Tenant.objects.create(name="WS One")
        self.t1_main = Site.objects.get(tenant=self.t1, is_default=True)
        self.t1_leic = Site.objects.create(tenant=self.t1, name="Leicester", site_type=Site.Type.CITY_BRANCH)
        self.t2 = T.objects.create(name="WS Two")
        self.t2_main = Site.objects.get(tenant=self.t2, is_default=True)
        self.user = User.objects.create_user("wsu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.t1, role="ADMIN", is_default=True)
        OrgMembership.objects.create(user=self.user, tenant=self.t2, role="ADMIN")
        self.client.login(username="wsu", password="pw")
        self._set_ctx(self.t1.id, self.t1_main.id)

    def _set_ctx(self, tid, sid):
        s = self.client.session
        s["active_tenant_id"] = tid
        s["active_site_id"] = sid
        s.save()

    def test_switch_both_company_and_site(self):
        from core.models import AuditLog
        resp = self.client.post("/switch-workspace/",
                                {"tenant": self.t2.id, "site": self.t2_main.id, "next": "/"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.session["active_tenant_id"], self.t2.id)
        self.assertEqual(self.client.session["active_site_id"], self.t2_main.id)
        self.assertTrue(AuditLog.objects.filter(action="COMPANY_SWITCHED").exists())
        self.assertTrue(AuditLog.objects.filter(action="SITE_SWITCHED").exists())

    def test_switch_site_within_same_company(self):
        from core.models import AuditLog
        resp = self.client.post("/switch-workspace/",
                                {"tenant": self.t1.id, "site": self.t1_leic.id, "next": "/inventory/"})
        self.assertRedirects(resp, "/inventory/", fetch_redirect_response=False)
        self.assertEqual(self.client.session["active_tenant_id"], self.t1.id)
        self.assertEqual(self.client.session["active_site_id"], self.t1_leic.id)
        # Company did not change -> no COMPANY_SWITCHED audit event.
        self.assertFalse(AuditLog.objects.filter(action="COMPANY_SWITCHED").exists())

    def test_unauthorised_company_403_and_no_change(self):
        from core.models import Tenant as T, Site
        other = T.objects.create(name="WS Stranger")
        other_site = Site.objects.filter(tenant=other, is_default=True).first()
        resp = self.client.post("/switch-workspace/", {"tenant": other.id, "site": other_site.id})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.client.session["active_tenant_id"], self.t1.id)  # unchanged

    def test_site_outside_company_403_and_no_change(self):
        # A site the user can access, but not within the posted company.
        resp = self.client.post("/switch-workspace/", {"tenant": self.t1.id, "site": self.t2_main.id})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.client.session["active_site_id"], self.t1_main.id)  # unchanged

    def test_context_lists_each_company_with_its_sites(self):
        resp = self.client.get("/", follow=True)
        wc = resp.context["workspace_companies"]
        sites_by_company = {w["tenant"].id: {s.id for s in w["sites"]} for w in wc}
        self.assertEqual(sites_by_company.get(self.t1.id), {self.t1_main.id, self.t1_leic.id})
        self.assertEqual(sites_by_company.get(self.t2.id), {self.t2_main.id})

    def test_dashboard_renders_switcher_modal(self):
        # The modal must render on the dashboard (in the body-level `modals`
        # block, so Bootstrap's fixed positioning isn't trapped by .skn-page).
        resp = self.client.get("/", follow=True)
        self.assertContains(resp, 'id="workspaceSwitcher"')
        self.assertContains(resp, 'action="/switch-workspace/"')


class SiteDataScopingTests(TestCase):
    """Module lists show only the selected site's data; switching site swaps it.
    There is no combined view."""

    def setUp(self):
        from core.models import (OrgMembership, InventoryBalance, CustomerOrder,
                                 CustomerOrderLine, Supplier, Site)
        self.tenant = Tenant.objects.create(name="Scope Co")
        Location.objects.filter(tenant=self.tenant).delete()
        Site.objects.filter(tenant=self.tenant).delete()
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        # Two sites, each with one inventory location.
        self.siteA = Site.objects.create(tenant=self.tenant, name="A Site", site_type=Site.Type.CITY_BRANCH)
        self.siteB = Site.objects.create(tenant=self.tenant, name="B Site", site_type=Site.Type.CITY_BRANCH)
        self.locA = Location.objects.create(tenant=self.tenant, site=self.siteA, name="A Warehouse", type=Location.Type.WAREHOUSE)
        self.locB = Location.objects.create(tenant=self.tenant, site=self.siteB, name="B Warehouse", type=Location.Type.WAREHOUSE)
        self.prod = Product.objects.create(tenant=self.tenant, sku="SC1", name="Widget")
        self.cust = Customer.objects.create(tenant=self.tenant, name="Cust")
        self.supplier = Supplier.objects.create(tenant=self.tenant, name="Supp")
        # Inventory at both sites' locations.
        InventoryBalance.objects.create(tenant=self.tenant, product=self.prod, location=self.locA, on_hand=Decimal("5"))
        InventoryBalance.objects.create(tenant=self.tenant, product=self.prod, location=self.locB, on_hand=Decimal("9"))
        # An invoice, order and PO whose location sits under each site.
        self.invA = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.cust, invoice_number="INV-A", location=self.locA)
        self.invB = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.cust, invoice_number="INV-B", location=self.locB)
        self.ordA = CustomerOrder.objects.create(tenant=self.tenant, customer=self.cust, order_number="SO-A", location=self.locA)
        self.ordB = CustomerOrder.objects.create(tenant=self.tenant, customer=self.cust, order_number="SO-B", location=self.locB)
        self.poA = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-A", supplier=self.supplier, receiving_location=self.locA)
        self.poB = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-B", supplier=self.supplier, receiving_location=self.locB)
        self.user = User.objects.create_user("scopeu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="scopeu", password="pw")  # auto-selects A Site (first)

    def _ctx_ids(self, url, key):
        return [o.id for o in self.client.get(url).context[key]]

    def test_lists_scoped_to_selected_site(self):
        self.assertEqual(self.client.session["active_site_id"], self.siteA.id)
        # Inventory (locations under the selected site)
        bals = self.client.get("/inventory/").context["balances"]
        self.assertEqual({b.location_id for b in bals}, {self.locA.id})
        # Invoices / orders / POs
        self.assertEqual(self._ctx_ids("/ar/invoices/", "invoices"), [self.invA.id])
        self.assertEqual(self._ctx_ids("/customer-orders/", "orders"), [self.ordA.id])
        self.assertEqual(self._ctx_ids("/po/", "pos"), [self.poA.id])

    def test_switching_site_swaps_data(self):
        self.client.post("/switch-site/", {"site": self.siteB.id})
        self.assertEqual(self.client.session["active_site_id"], self.siteB.id)
        bals = self.client.get("/inventory/").context["balances"]
        self.assertEqual({b.location_id for b in bals}, {self.locB.id})
        self.assertEqual(self._ctx_ids("/ar/invoices/", "invoices"), [self.invB.id])
        self.assertEqual(self._ctx_ids("/customer-orders/", "orders"), [self.ordB.id])
        self.assertEqual(self._ctx_ids("/po/", "pos"), [self.poB.id])


class DefaultSiteTests(TestCase):
    """A fresh company gets a default Site, and the Main Location sits under it."""

    def test_new_tenant_gets_default_site_with_location(self):
        from core.models import Site, Location
        t = Tenant.objects.create(name="Site Boot Co")
        sites = Site.objects.filter(tenant=t)
        self.assertEqual(sites.count(), 1)
        site = sites.get()
        self.assertEqual(site.name, "Main Site")
        self.assertTrue(site.is_default)
        self.assertEqual(site.site_type, Site.Type.OPERATING_SITE)
        loc = Location.objects.get(tenant=t, name="Main Location")
        self.assertEqual(loc.site_id, site.id)  # location belongs to the site

    def test_existing_location_without_site_is_attached(self):
        from core.models import Site, Location
        t = Tenant.objects.create(name="Orphan Co")
        # Simulate a legacy orphan location (no site).
        orphan = Location.objects.create(tenant=t, name="Legacy WH", type=Location.Type.WAREHOUSE)
        orphan.site = None
        orphan.save(update_fields=["site"])
        # The backfill helper (same logic as migration 0066) re-attaches it.
        site = Site.objects.filter(tenant=t, is_default=True).first()
        Location.objects.filter(tenant=t, site__isnull=True).update(site=site)
        orphan.refresh_from_db()
        self.assertEqual(orphan.site_id, site.id)

    def test_site_type_choices_present(self):
        from core.models import Site
        keys = set(dict(Site.Type.choices))
        self.assertEqual(keys, {"region", "country_division", "city_branch", "business_unit", "operating_site"})


class SiteAccessTests(TestCase):
    """UserSiteAccess gates which sites a user may work in (distinct from
    location access)."""

    def setUp(self):
        from core.models import OrgMembership, Site
        self.tenant = Tenant.objects.create(name="SA Co")
        self.main = Site.objects.get(tenant=self.tenant, is_default=True)
        self.leic = Site.objects.create(tenant=self.tenant, name="Leicester", site_type=Site.Type.CITY_BRANCH)
        self.lon = Site.objects.create(tenant=self.tenant, name="London", site_type=Site.Type.CITY_BRANCH)
        self.admin = User.objects.create_user("saadmin", password="pw")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)
        self.staff = User.objects.create_user("sastaff", password="pw")
        OrgMembership.objects.create(user=self.staff, tenant=self.tenant, role="WAREHOUSE")

    def test_no_grants_unrestricted(self):
        from core.access import accessible_site_ids
        self.assertIsNone(accessible_site_ids(self.staff, self.tenant))

    def test_admin_unrestricted_even_with_grants(self):
        from core.access import accessible_site_ids
        from core.models import UserSiteAccess
        UserSiteAccess.objects.create(tenant=self.tenant, user=self.admin, site=self.leic)
        self.assertIsNone(accessible_site_ids(self.admin, self.tenant))

    def test_grants_restrict_staff(self):
        from core.access import accessible_site_ids, selectable_sites
        from core.models import UserSiteAccess
        UserSiteAccess.objects.create(tenant=self.tenant, user=self.staff, site=self.leic)
        self.assertEqual(accessible_site_ids(self.staff, self.tenant), {self.leic.id})
        names = set(selectable_sites(self.staff, self.tenant).values_list("name", flat=True))
        self.assertEqual(names, {"Leicester"})

    def test_selectable_sites_excludes_inactive(self):
        from core.access import selectable_sites
        self.lon.is_active = False
        self.lon.save()
        names = set(selectable_sites(self.admin, self.tenant).values_list("name", flat=True))
        self.assertEqual(names, {"Main Site", "Leicester"})

    def test_site_access_scoped_to_tenant(self):
        from core.access import accessible_site_ids
        from core.models import UserSiteAccess, Tenant as T
        other = T.objects.create(name="SA Other")
        # A grant in another tenant must not leak.
        UserSiteAccess.objects.create(tenant=self.tenant, user=self.staff, site=self.leic)
        self.assertIsNone(accessible_site_ids(self.staff, other))  # no grants there -> unrestricted in 'other'


class ActiveSiteResolutionTests(TestCase):
    """get_active_site resolves from the session, validates access, and falls
    back to the selected location's site during the transition."""

    def setUp(self):
        from core.models import OrgMembership, Site
        self.tenant = Tenant.objects.create(name="AS Co")
        self.main_site = Site.objects.get(tenant=self.tenant, is_default=True)
        self.main_loc = Location.objects.get(tenant=self.tenant, name="Main Location")
        self.user = User.objects.create_user("asu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)

    def _request(self, **session):
        from django.test import RequestFactory
        from django.contrib.sessions.backends.db import SessionStore
        req = RequestFactory().get("/")
        req.user = self.user
        s = SessionStore()
        for k, v in session.items():
            s[k] = v
        s.save()
        req.session = s
        return req

    def test_explicit_site_selected(self):
        from core.access import get_active_site, SESSION_TENANT_KEY, SESSION_SITE_KEY
        req = self._request(**{SESSION_TENANT_KEY: self.tenant.id, SESSION_SITE_KEY: self.main_site.id})
        self.assertEqual(get_active_site(req), self.main_site)

    def test_fallback_from_selected_location(self):
        from core.access import get_active_site, SESSION_TENANT_KEY, SESSION_LOCATION_KEY
        # No site key, but a location is selected -> derive its site.
        req = self._request(**{SESSION_TENANT_KEY: self.tenant.id, SESSION_LOCATION_KEY: self.main_loc.id})
        self.assertEqual(get_active_site(req), self.main_site)

    def test_invalid_site_not_returned(self):
        from core.access import get_active_site, SESSION_TENANT_KEY, SESSION_SITE_KEY
        from core.models import Site, Tenant as T
        other = T.objects.create(name="AS Other")
        foreign = Site.objects.filter(tenant=other).first()
        req = self._request(**{SESSION_TENANT_KEY: self.tenant.id, SESSION_SITE_KEY: foreign.id})
        self.assertIsNone(get_active_site(req))  # not in this company -> not returned


class StockSiteAndWorkflowTests(TestCase):
    """Stock balances/movements carry site_id; inventory workflow location
    pickers are scoped to the selected site (cross-site transfer destinations
    stay open)."""

    def setUp(self):
        from core.models import OrgMembership, Site
        self.tenant = Tenant.objects.create(name="S6 Co")
        Location.objects.filter(tenant=self.tenant).delete()
        Site.objects.filter(tenant=self.tenant).delete()
        self.siteA = Site.objects.create(tenant=self.tenant, name="A Site", site_type=Site.Type.CITY_BRANCH, is_default=True)
        self.siteB = Site.objects.create(tenant=self.tenant, name="B Site", site_type=Site.Type.CITY_BRANCH)
        self.locA = Location.objects.create(tenant=self.tenant, site=self.siteA, name="A WH", type=Location.Type.WAREHOUSE)
        self.locB = Location.objects.create(tenant=self.tenant, site=self.siteB, name="B WH", type=Location.Type.WAREHOUSE)
        self.prod = Product.objects.create(tenant=self.tenant, sku="S6-1", name="Widget")
        self.user = User.objects.create_user("s6u", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="s6u", password="pw")  # auto-selects A Site

    def test_movement_and_balance_record_site(self):
        from core.services.inventory import apply_movement
        from core.models import InventoryBalance, InventoryMovement
        apply_movement(tenant=self.tenant, product=self.prod, location=self.locB,
                       movement_type="RECEIVE", qty_delta=Decimal("10"), ref_type="SEED", ref_id="1",
                       unit_cost=Decimal("2"))
        bal = InventoryBalance.objects.get(tenant=self.tenant, product=self.prod, location=self.locB)
        self.assertEqual(bal.site_id, self.siteB.id)  # derived from location.site
        mv = InventoryMovement.objects.filter(tenant=self.tenant, location=self.locB).first()
        self.assertEqual(mv.site_id, self.siteB.id)

    def test_adjustment_location_picker_scoped_to_active_site(self):
        from core.forms import StockAdjustmentForm
        # Active site is A Site -> adjustment location options limited to A's locations.
        resp = self.client.get("/inventory/adjustments/new/")
        form = resp.context["form"]
        ids = set(form.fields["location"].queryset.values_list("id", flat=True))
        self.assertEqual(ids, {self.locA.id})

    def test_transfer_from_is_site_scoped_to_is_open(self):
        from core.forms import InventoryTransferForm
        resp = self.client.get("/transfers/new/")
        form = resp.context["form"]
        from_ids = set(form.fields["from_location"].queryset.values_list("id", flat=True))
        to_ids = set(form.fields["to_location"].queryset.values_list("id", flat=True))
        self.assertEqual(from_ids, {self.locA.id})            # within the working site
        self.assertEqual(to_ids, {self.locA.id, self.locB.id})  # cross-site allowed


class DocumentSiteScopingTests(TestCase):
    """Sales orders/invoices and POs carry site_id (stamped from the active site
    on create, or derived), and lists scope by it."""

    def setUp(self):
        from core.models import OrgMembership, Site, Supplier
        self.tenant = Tenant.objects.create(name="Doc Co")
        Location.objects.filter(tenant=self.tenant).delete()
        Site.objects.filter(tenant=self.tenant).delete()
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.siteA = Site.objects.create(tenant=self.tenant, name="A Site", site_type=Site.Type.CITY_BRANCH, is_default=True)
        self.siteB = Site.objects.create(tenant=self.tenant, name="B Site", site_type=Site.Type.CITY_BRANCH)
        self.locA = Location.objects.create(tenant=self.tenant, site=self.siteA, name="A WH", type=Location.Type.WAREHOUSE)
        self.locB = Location.objects.create(tenant=self.tenant, site=self.siteB, name="B WH", type=Location.Type.WAREHOUSE)
        self.cust = Customer.objects.create(tenant=self.tenant, name="C")
        self.supplier = Supplier.objects.create(tenant=self.tenant, name="S")
        self.prod = Product.objects.create(tenant=self.tenant, sku="DOC1", name="W")
        self.user = User.objects.create_user("docu", password="pw")
        OrgMembership.objects.create(user=self.user, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="docu", password="pw")  # auto-selects A Site

    def test_invoice_site_derived_from_location(self):
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.cust,
                                             invoice_number="INV-1", location=self.locB)
        self.assertEqual(inv.site_id, self.siteB.id)

    def test_document_without_location_falls_back_to_default_site(self):
        # No location -> default Site (A Site is default).
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.cust, invoice_number="INV-2")
        self.assertEqual(inv.site_id, self.siteA.id)

    def test_create_view_stamps_active_site(self):
        # Switch to B Site, then create an invoice via the view -> stamped to B.
        self.client.post("/switch-site/", {"site": self.siteB.id})
        resp = self.client.post("/ar/invoices/new/", {
            "customer": self.cust.id, "invoice_date": "2026-01-01", "action": "save",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": self.prod.id, "lines-0-qty": "1", "lines-0-unit_price": "10",
            "lines-0-tax_code": self.std.id,
        })
        self.assertEqual(resp.status_code, 302)
        inv = CustomerInvoice.objects.get(tenant=self.tenant, customer=self.cust)
        self.assertEqual(inv.site_id, self.siteB.id)

    def test_po_list_scoped_by_site(self):
        poA = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-A", supplier=self.supplier, receiving_location=self.locA)
        poB = PurchaseOrder.objects.create(tenant=self.tenant, po_number="PO-B", supplier=self.supplier, receiving_location=self.locB)
        ids = [p.id for p in self.client.get("/po/").context["pos"]]  # A Site active
        self.assertEqual(ids, [poA.id])


class UKSeedTests(TestCase):
    """The seed_uk_demo command builds Company UK -> city/region Sites -> named
    inventory locations, idempotently."""

    def test_seed_creates_sites_and_locations(self):
        from django.core.management import call_command
        from core.models import Tenant, Site, Location
        call_command("seed_uk_demo")
        call_command("seed_uk_demo")  # idempotent
        uk = Tenant.objects.get(name="UK")
        names = set(Site.objects.filter(tenant=uk).values_list("name", flat=True))
        for s in ["London", "Leicester", "Manchester", "Birmingham",
                  "England", "Wales", "Scotland", "Northern Ireland"]:
            self.assertIn(s, names)
        leic = Site.objects.get(tenant=uk, name="Leicester")
        leic_locs = set(Location.objects.filter(tenant=uk, site=leic).values_list("name", flat=True))
        self.assertEqual(leic_locs, {
            "Leicester Main Warehouse", "Leicester Shop Floor", "Leicester Back Room",
            "Leicester Returns Area", "Leicester Delivery Van"})
        # Region sites carry the region type.
        self.assertEqual(Site.objects.get(tenant=uk, name="Scotland").site_type, Site.Type.REGION)
        self.assertEqual(leic.site_type, Site.Type.CITY_BRANCH)


class SiteAccessMatrixTests(TestCase):
    """Admin UI to grant per-site access (UserSiteAccess), with audit."""

    def setUp(self):
        from core.models import OrgMembership, Site
        self.tenant = Tenant.objects.create(name="SAM Co")
        self.main = Site.objects.get(tenant=self.tenant, is_default=True)
        self.leic = Site.objects.create(tenant=self.tenant, name="Leicester", site_type=Site.Type.CITY_BRANCH)
        self.admin = User.objects.create_user("samadmin", password="pw")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)
        self.staff = User.objects.create_user("samstaff", password="pw")
        OrgMembership.objects.create(user=self.staff, tenant=self.tenant, role="WAREHOUSE")

    def test_admin_saves_site_grants_and_audits(self):
        from core.models import UserSiteAccess, AuditLog
        self.client.login(username="samadmin", password="pw")
        self.assertEqual(self.client.get("/sites/access/").status_code, 200)
        resp = self.client.post("/sites/access/", {f"grant_{self.staff.id}_{self.leic.id}": "on"})
        self.assertEqual(resp.status_code, 302)
        grants = set(UserSiteAccess.objects.filter(tenant=self.tenant, user=self.staff).values_list("site_id", flat=True))
        self.assertEqual(grants, {self.leic.id})
        self.assertTrue(AuditLog.objects.filter(tenant=self.tenant, action="SITE_ACCESS_CHANGED").exists())

    def test_non_admin_forbidden(self):
        self.client.login(username="samstaff", password="pw")
        self.assertEqual(self.client.get("/sites/access/").status_code, 403)

    def test_grant_restricts_site_picker(self):
        from core.models import UserSiteAccess
        from core.access import selectable_sites
        UserSiteAccess.objects.create(tenant=self.tenant, user=self.staff, site=self.leic)
        names = set(selectable_sites(self.staff, self.tenant).values_list("name", flat=True))
        self.assertEqual(names, {"Leicester"})  # restricted to the granted site


class GLSiteDimensionTests(TestCase):
    """Journal entries carry a Site (from their source document) so P&L /
    balance sheet can be filtered by site; company-wide is the default."""

    def setUp(self):
        from core.models import OrgMembership, Site, InventoryMovement, GLAccount
        from core.services.inventory import apply_movement
        self.tenant = Tenant.objects.create(name="GLS Co")
        Location.objects.filter(tenant=self.tenant).delete()
        Site.objects.filter(tenant=self.tenant).delete()
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.siteA = Site.objects.create(tenant=self.tenant, name="A Site", site_type=Site.Type.CITY_BRANCH, is_default=True)
        self.siteB = Site.objects.create(tenant=self.tenant, name="B Site", site_type=Site.Type.CITY_BRANCH)
        self.locA = Location.objects.create(tenant=self.tenant, site=self.siteA, name="A WH", type=Location.Type.WAREHOUSE)
        self.locB = Location.objects.create(tenant=self.tenant, site=self.siteB, name="B WH", type=Location.Type.WAREHOUSE)
        self.cust = Customer.objects.create(tenant=self.tenant, name="C")
        self.prod = Product.objects.create(tenant=self.tenant, sku="GLS1", name="W",
                                            cost_method=Product.CostMethod.AVERAGE)
        for loc in (self.locA, self.locB):
            apply_movement(tenant=self.tenant, product=self.prod, location=loc,
                           movement_type=InventoryMovement.MovementType.RECEIVE, qty_delta=Decimal("100"),
                           ref_type="SEED", ref_id=f"O{loc.id}", unit_cost=Decimal("4.00"))

    def _invoice(self, number, location, qty, price):
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.cust,
                                             invoice_number=number, location=location)
        CustomerInvoiceLine.objects.create(invoice=inv, product=self.prod, qty=Decimal(qty),
                                           unit_price=Decimal(price), tax_code=self.std)
        post_customer_invoice(inv)
        return inv

    def test_invoice_je_carries_site(self):
        from core.models import JournalEntry
        self._invoice("INV-A", self.locA, "10", "25")
        for ref in ("AR_INVOICE", "COGS"):
            je = JournalEntry.objects.get(tenant=self.tenant, ref_type=ref, ref_id="INV-A")
            self.assertEqual(je.site_id, self.siteA.id)

    def test_pnl_filtered_by_site(self):
        from core.services import reports
        self._invoice("INV-A", self.locA, "10", "25")  # site A: revenue 250, COGS 40
        self._invoice("INV-B", self.locB, "4", "25")   # site B: revenue 100, COGS 16
        company = reports.profit_and_loss(self.tenant)
        self.assertEqual(company["income_total"], Decimal("350.00"))  # combined default
        a = reports.profit_and_loss(self.tenant, site_ids=[self.siteA.id])
        self.assertEqual(a["income_total"], Decimal("250.00"))
        self.assertEqual(a["cogs_total"], Decimal("40.00"))
        b = reports.profit_and_loss(self.tenant, site_ids=[self.siteB.id])
        self.assertEqual(b["income_total"], Decimal("100.00"))

    def test_expense_je_carries_site(self):
        from core.models import Expense, GLAccount, JournalEntry
        from core.services.gl import post_expense
        acc = GLAccount.objects.filter(tenant=self.tenant, code="6100").first()
        e = Expense.objects.create(tenant=self.tenant, site=self.siteB, payee="Rent Co",
                                   category=acc, net_amount=Decimal("100.00"))
        post_expense(e)
        je = JournalEntry.objects.get(tenant=self.tenant, ref_type="EXPENSE", ref_id=str(e.id))
        self.assertEqual(je.site_id, self.siteB.id)
        from core.services import reports
        b = reports.profit_and_loss(self.tenant, site_ids=[self.siteB.id])
        self.assertEqual(b["expense_total"], Decimal("100.00"))
        a = reports.profit_and_loss(self.tenant, site_ids=[self.siteA.id])
        self.assertEqual(a["expense_total"], Decimal("0.00"))

    def test_pnl_page_site_filter(self):
        from core.models import OrgMembership
        u = User.objects.create_user("glsu", password="pw")
        OrgMembership.objects.create(user=u, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="glsu", password="pw")
        self._invoice("INV-A", self.locA, "10", "25")
        self._invoice("INV-B", self.locB, "4", "25")
        # Company-wide (default).
        resp = self.client.get("/reports/profit-and-loss/?from=2026-01-01&to=2026-12-31")
        self.assertEqual(resp.context["data"]["income_total"], Decimal("350.00"))
        # Filtered to site A.
        resp = self.client.get(f"/reports/profit-and-loss/?from=2026-01-01&to=2026-12-31&site={self.siteA.id}")
        self.assertEqual(resp.context["data"]["income_total"], Decimal("250.00"))


class AuditSiteTests(TestCase):
    """Audit records capture the working Site, and the log can be filtered by it."""

    def setUp(self):
        from core.models import OrgMembership, Site
        self.tenant = Tenant.objects.create(name="AudSite Co")
        self.main = Site.objects.get(tenant=self.tenant, is_default=True)
        self.leic = Site.objects.create(tenant=self.tenant, name="Leicester", site_type=Site.Type.CITY_BRANCH)
        self.admin = User.objects.create_user("audadmin", password="pw")
        OrgMembership.objects.create(user=self.admin, tenant=self.tenant, role="ADMIN", is_default=True)

    def test_log_audit_stamps_current_site(self):
        from core.models import AuditLog
        from core.current import set_current_site, clear_current_site
        from core.audit import log_audit
        set_current_site(self.leic)
        try:
            log_audit(action="DATA_EXPORTED", tenant=self.tenant, username="x", detail="test")
        finally:
            clear_current_site()
        log = AuditLog.objects.filter(tenant=self.tenant, action="DATA_EXPORTED").first()
        self.assertEqual(log.site_id, self.leic.id)

    def test_explicit_site_overrides_threadlocal(self):
        from core.models import AuditLog
        from core.audit import log_audit
        log_audit(action="RECORD_DELETED", tenant=self.tenant, site=self.main, username="x")
        log = AuditLog.objects.filter(tenant=self.tenant, action="RECORD_DELETED").first()
        self.assertEqual(log.site_id, self.main.id)

    def test_audit_log_view_filters_by_site(self):
        from core.models import AuditLog
        AuditLog.objects.create(tenant=self.tenant, site=self.leic, action="LOGIN", username="a")
        AuditLog.objects.create(tenant=self.tenant, site=self.main, action="LOGIN", username="b")
        self.client.login(username="audadmin", password="pw")
        resp = self.client.get(f"/audit/?site={self.leic.id}")
        self.assertEqual(resp.status_code, 200)
        usernames = {l.username for l in resp.context["logs"]}
        self.assertIn("a", usernames)
        self.assertNotIn("b", usernames)


class DashboardSiteScopingTests(TestCase):
    """Dashboard KPIs are scoped to the selected site (site-dimensioned data)."""

    def setUp(self):
        from core.models import OrgMembership, Site, InventoryMovement
        from core.services.inventory import apply_movement
        self.tenant = Tenant.objects.create(name="DashSite Co")
        Location.objects.filter(tenant=self.tenant).delete()
        Site.objects.filter(tenant=self.tenant).delete()
        self.std = TaxCode.objects.get(tenant=self.tenant, code="STD")
        self.siteA = Site.objects.create(tenant=self.tenant, name="A Site", site_type=Site.Type.CITY_BRANCH, is_default=True)
        self.siteB = Site.objects.create(tenant=self.tenant, name="B Site", site_type=Site.Type.CITY_BRANCH)
        self.locA = Location.objects.create(tenant=self.tenant, site=self.siteA, name="A WH", type=Location.Type.WAREHOUSE)
        self.locB = Location.objects.create(tenant=self.tenant, site=self.siteB, name="B WH", type=Location.Type.WAREHOUSE)
        self.cust = Customer.objects.create(tenant=self.tenant, name="C")
        self.prod = Product.objects.create(tenant=self.tenant, sku="DS1", name="W", cost_method=Product.CostMethod.AVERAGE)
        for loc in (self.locA, self.locB):
            apply_movement(tenant=self.tenant, product=self.prod, location=loc,
                           movement_type=InventoryMovement.MovementType.RECEIVE, qty_delta=Decimal("100"),
                           ref_type="SEED", ref_id=f"O{loc.id}", unit_cost=Decimal("4.00"))

    def _invoice(self, number, loc, qty, price):
        inv = CustomerInvoice.objects.create(tenant=self.tenant, customer=self.cust, invoice_number=number, location=loc)
        CustomerInvoiceLine.objects.create(invoice=inv, product=self.prod, qty=Decimal(qty), unit_price=Decimal(price), tax_code=self.std)
        post_customer_invoice(inv)
        return inv

    def test_kpis_scoped_to_site(self):
        from core.views import _dashboard_kpis
        from core.access import accessible_locations
        self._invoice("INV-A", self.locA, "10", "25")  # site A this month
        self._invoice("INV-B", self.locB, "4", "25")   # site B this month
        a_locs = list(accessible_locations(None, self.tenant).filter(site=self.siteA).values_list("id", flat=True))
        kA = {c["label"]: c["value"] for c in _dashboard_kpis(self.tenant, "ADMIN", site_id=self.siteA.id, location_ids=a_locs)}
        # "Sales (this month)" should reflect only site A's revenue.
        self.assertEqual(kA["Sales (this month)"], Decimal("250.00"))
        company = {c["label"]: c["value"] for c in _dashboard_kpis(self.tenant, "ADMIN")}
        self.assertEqual(company["Sales (this month)"], Decimal("350.00"))

    def test_dashboard_page_renders_with_site(self):
        from core.models import OrgMembership
        u = User.objects.create_user("dsu", password="pw")
        OrgMembership.objects.create(user=u, tenant=self.tenant, role="ADMIN", is_default=True)
        self.client.login(username="dsu", password="pw")  # auto-selects A Site
        resp = self.client.get("/dashboard/admin")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["active_site"], self.siteA)
