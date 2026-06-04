from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase, Client

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
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/select-org/")

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
