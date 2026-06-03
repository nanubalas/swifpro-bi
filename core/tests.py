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

    def test_bank_reconciliation_toggles_cleared(self):
        from core.models import Payment
        self.client.post("/payments/receipts/new/", {
            "customer": self.customer.id, "payment_date": "2026-05-30",
            "amount": "240.00", "method": "BANK",
        })
        payment = Payment.objects.get(tenant=self.tenant)
        self.assertFalse(payment.is_reconciled)

        resp = self.client.post("/bank/reconcile/", {"cleared": [str(payment.id)]})
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
                     "/reports/balance-sheet/", "/reports/aged-receivables/", "/reports/aged-payables/"]:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, f"{path} -> {resp.status_code}")


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
