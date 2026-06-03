from decimal import Decimal

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User, Group
from django.db import transaction
from django.utils import timezone

from core.auth import ALL_ROLES
from core.models import (
    Tenant, UserProfile, Location, Supplier, UnitOfMeasure, Product,
    BillOfMaterials, BillOfMaterialsLine, PurchaseOrder, PurchaseOrderLine,
    Shipment, ShipmentLine, InventoryMovement, Customer, CustomerInvoice,
    CustomerInvoiceLine, TaxCode, SalesOrder, SalesOrderLine, ChannelConnection,
    SalesChannel, SyncRun, Payment, PaymentAllocation, OrgMembership,
    AuditLog, UserPermissionOverride, GLAccount, Expense, CreditNote, CreditNoteLine,
    BankTransaction,
)
from core.services.gl import post_expense, post_credit_note
from core import permissions as permissions_mod
from core import roles as roles_mod
from core.services.inventory import apply_movement
from core.services.gl import post_customer_invoice, post_inventory_receipt, post_payment
from core.services.sync_shopify import sync_shopify_for_tenant
from core.views import _post_sales_order


class Command(BaseCommand):
    help = "Seed a demo tenant with sample data so the UI is fully populated."

    @transaction.atomic
    def handle(self, *args, **options):
        # Role groups (so non-superusers can be granted access later)
        for role in ALL_ROLES:
            Group.objects.get_or_create(name=role)

        # Tenant - the post_save signal bootstraps VAT codes + GL accounts.
        tenant, created = Tenant.objects.get_or_create(
            name="SwifPro BI Demo Ltd",
            defaults={
                "email": "ops@swifpro-demo.co.uk",
                "phone": "+44 20 7946 0000",
                "vat_number": "GB123456789",
                "company_number": "12345678",
                "currency_code": "GBP",
                "po_approval_threshold": Decimal("5000.00"),
            },
        )
        # Full company profile (idempotent).
        tenant.legal_name = "SwifPro BI Demo Limited"
        tenant.trading_name = "SwifPro BI"
        tenant.business_type = Tenant.BusinessType.LTD
        tenant.utr_number = "1234567890"
        tenant.vat_registered = True
        tenant.vat_number = "GB123456789"
        tenant.address_line1 = "Unit 4, Innovation Park"
        tenant.address_line2 = "Charter Street"
        tenant.address_city = "Manchester"
        tenant.address_postcode = "M1 2AB"
        tenant.address_country = "United Kingdom"
        tenant.billing_same_as_business = True
        tenant.email = "ops@swifpro-demo.co.uk"
        tenant.phone = "+44 161 555 0100"
        tenant.website = "https://swifprobi.com"
        tenant.invoice_footer = "Thank you for your business. Payment due within terms. SwifPro BI Demo Limited - Reg. 12345678."
        tenant.country = "United Kingdom"
        tenant.timezone = "Europe/London"
        tenant.financial_year_start_month = 4
        tenant.default_payment_terms_days = 30
        tenant.onboarding_complete = True
        std = TaxCode.objects.filter(tenant=tenant, code="STD").first()
        if std:
            tenant.default_tax_code = std
        tenant.save()

        # Company logo (copy the brand asset into media if not already set).
        if not tenant.logo:
            import os
            from django.conf import settings
            from django.core.files import File
            src = os.path.join(settings.BASE_DIR, "core", "static", "img", "logo.png")
            if os.path.exists(src):
                with open(src, "rb") as fh:
                    tenant.logo.save("demo-logo.png", File(fh), save=True)

        self.stdout.write(("Created" if created else "Reusing") + f" tenant: {tenant.name} (profile filled)")

        # Bind the admin user to this tenant (if present)
        admin = User.objects.filter(username="admin").first()
        if admin:
            UserProfile.objects.update_or_create(user=admin, defaults={"tenant": tenant})
            OrgMembership.objects.get_or_create(user=admin, tenant=tenant, defaults={"role": roles_mod.ADMIN, "is_default": True})

        # Sample users, one per role (password: Skunow@2026)
        role_users = [
            ("owner", roles_mod.ADMIN),
            ("accountant", roles_mod.ACCOUNTANT),
            ("manager", roles_mod.MANAGER),
            ("sales", roles_mod.SALES),
            ("warehouse", roles_mod.WAREHOUSE),
            ("purchasing", roles_mod.PURCHASING),
            ("finance", roles_mod.FINANCE),
            ("viewer", roles_mod.READONLY),
        ]
        for username, role in role_users:
            u, created = User.objects.get_or_create(username=username, defaults={"email": f"{username}@swifpro-demo.co.uk"})
            if created:
                u.set_password("Skunow@2026")
                u.save()
            UserProfile.objects.update_or_create(user=u, defaults={"tenant": tenant})
            OrgMembership.objects.get_or_create(user=u, tenant=tenant, defaults={"role": role, "is_default": True})
        self.stdout.write("Seeded role users (owner/accountant/manager/sales/warehouse/purchasing/finance/viewer).")

        # Multi-org demo: a second organisation where 'manager' is the Accountant.
        north, _ = Tenant.objects.get_or_create(
            name="SwifPro BI North Ltd",
            defaults={"currency_code": "GBP", "email": "north@swifpro-demo.co.uk"},
        )
        mgr = User.objects.filter(username="manager").first()
        if mgr:
            OrgMembership.objects.get_or_create(user=mgr, tenant=north, defaults={"role": roles_mod.ACCOUNTANT})
            self.stdout.write("Added 'manager' as Accountant in SwifPro BI North Ltd (multi-org demo).")

        # UOM
        each, _ = UnitOfMeasure.objects.get_or_create(tenant=tenant, code="each", defaults={"name": "Each"})

        # Locations
        wh, _ = Location.objects.get_or_create(tenant=tenant, name="Main Warehouse", defaults={"type": Location.Type.WAREHOUSE})
        Location.objects.get_or_create(tenant=tenant, name="3PL London", defaults={"type": Location.Type.THREEPL})
        Location.objects.get_or_create(tenant=tenant, name="Returns", defaults={"type": Location.Type.RETURNS})

        # Suppliers
        globex, _ = Supplier.objects.get_or_create(tenant=tenant, name="Globex Supplies", defaults={"email": "sales@globex.example", "currency_code": "GBP"})
        Supplier.objects.get_or_create(tenant=tenant, name="Acme Imports", defaults={"email": "orders@acme.example", "currency_code": "USD"})

        # Products
        def make_product(sku, name, cost):
            return Product.objects.get_or_create(
                tenant=tenant, sku=sku,
                defaults={"name": name, "base_uom": each, "uom": "each",
                          "standard_cost": Decimal(cost), "cost_method": Product.CostMethod.AVERAGE},
            )[0]

        p1 = make_product("SKU-001", "Aurora Desk Lamp", "12.50")
        p2 = make_product("SKU-002", "Nimbus Wireless Mouse", "8.00")
        p3 = make_product("SKU-003", "Halcyon Notebook A5", "3.25")
        kit = make_product("SKU-KIT-001", "Home Office Starter Kit", "0.00")

        # BOM / kit: starter kit = 1 lamp + 1 mouse + 2 notebooks
        bom, _ = BillOfMaterials.objects.get_or_create(tenant=tenant, product=kit, name="Default BOM")
        BillOfMaterialsLine.objects.get_or_create(bom=bom, component=p1, defaults={"qty": Decimal("1"), "uom": each})
        BillOfMaterialsLine.objects.get_or_create(bom=bom, component=p2, defaults={"qty": Decimal("1"), "uom": each})
        BillOfMaterialsLine.objects.get_or_create(bom=bom, component=p3, defaults={"qty": Decimal("2"), "uom": each})

        # Seed inventory once (guard so re-running doesn't double balances)
        if not InventoryMovement.objects.filter(tenant=tenant, ref_type="SEED").exists():
            opening_value = Decimal("0.00")
            for product, qty in [(p1, "120"), (p2, "80"), (p3, "200")]:
                apply_movement(
                    tenant=tenant, product=product, location=wh,
                    movement_type=InventoryMovement.MovementType.RECEIVE,
                    qty_delta=Decimal(qty), ref_type="SEED", ref_id="INIT",
                    notes="Opening balance (demo seed)",
                    unit_cost=product.standard_cost,  # sets moving-average cost
                )
                opening_value += Decimal(qty) * product.standard_cost
            # Capitalize opening stock to the GL so the Balance Sheet shows it.
            post_inventory_receipt(tenant, opening_value, "OPENING")
            self.stdout.write("Seeded opening inventory balances (capitalized to GL).")

        # Customer
        Customer.objects.get_or_create(
            tenant=tenant, name="Bright Retail Ltd",
            defaults={"email": "ap@brightretail.example", "vat_number": "GB987654321",
                      "billing_address": "10 High Street\nManchester\nM1 1AA"},
        )

        # Purchase order (submitted) + shipment + lines
        po, po_created = PurchaseOrder.objects.get_or_create(
            tenant=tenant, po_number="PO-DEMO-0001",
            defaults={"supplier": globex, "currency_code": "GBP",
                      "status": PurchaseOrder.Status.SUBMITTED,
                      "expected_date": (timezone.now() + timezone.timedelta(days=10)).date(),
                      "notes": "Demo purchase order."},
        )
        if po_created:
            pol1 = PurchaseOrderLine.objects.create(po=po, product=p1, ordered_qty=Decimal("50"), unit_cost=Decimal("12.50"))
            pol2 = PurchaseOrderLine.objects.create(po=po, product=p2, ordered_qty=Decimal("100"), unit_cost=Decimal("8.00"))
            shipment = Shipment.objects.create(tenant=tenant, po=po, from_supplier=globex, destination=wh, carrier="DHL", tracking_number="DEMO123456", status=Shipment.Status.IN_TRANSIT)
            ShipmentLine.objects.create(shipment=shipment, po_line=pol1, expected_qty=Decimal("50"))
            ShipmentLine.objects.create(shipment=shipment, po_line=pol2, expected_qty=Decimal("100"))
            self.stdout.write("Created demo PO + inbound shipment.")

        # Sales order (draft) - exercises kit explosion / reservations later
        so, so_created = SalesOrder.objects.get_or_create(
            tenant=tenant, channel=SalesChannel.SHOPIFY, order_number="SO-DEMO-0001",
            defaults={"status": SalesOrder.Status.DRAFT, "ship_from_location": wh, "currency_code": "GBP"},
        )
        if so_created:
            SalesOrderLine.objects.create(order=so, product=p1, qty=Decimal("3"), unit_price=Decimal("24.99"))
            SalesOrderLine.objects.create(order=so, product=kit, qty=Decimal("1"), unit_price=Decimal("49.99"))
            # Post it so inventory is deducted and COGS is expensed to the GL.
            _post_sales_order(so)
            self.stdout.write("Created + posted demo sales order (COGS expensed).")

        # AR invoice - issue it so it posts a balanced journal to the GL
        std_tax = TaxCode.objects.filter(tenant=tenant, code="STD").first()
        customer = Customer.objects.get(tenant=tenant, name="Bright Retail Ltd")
        inv, inv_created = CustomerInvoice.objects.get_or_create(
            tenant=tenant, invoice_number="INV-DEMO-0001",
            defaults={"customer": customer, "currency_code": "GBP",
                      "due_date": (timezone.now() + timezone.timedelta(days=30)).date()},
        )
        if inv_created:
            CustomerInvoiceLine.objects.create(invoice=inv, product=p1, description="Aurora Desk Lamp", qty=Decimal("10"), unit_price=Decimal("24.99"), tax_code=std_tax)
            CustomerInvoiceLine.objects.create(invoice=inv, product=p2, description="Nimbus Wireless Mouse", qty=Decimal("20"), unit_price=Decimal("14.99"), tax_code=std_tax)
            post_customer_invoice(inv, user=admin)
            self.stdout.write("Created + issued demo AR invoice (posted to GL).")

        # A part-payment receipt against that invoice (populates payments + bank rec)
        if not Payment.objects.filter(tenant=tenant).exists():
            receipt = Payment.objects.create(
                tenant=tenant, direction=Payment.Direction.RECEIPT, customer=customer,
                amount=Decimal("300.00"), method=Payment.Method.BANK, reference="FPS-DEMO-1",
                currency_code="GBP",
            )
            PaymentAllocation.objects.create(payment=receipt, customer_invoice=inv, amount=Decimal("300.00"))
            post_payment(receipt, user=admin)
            self.stdout.write("Recorded a demo customer receipt (part payment).")

        # Shopify channel + a sync run (populates snapshot + sales movements)
        ChannelConnection.objects.get_or_create(
            tenant=tenant, channel=SalesChannel.SHOPIFY, name="default",
            defaults={"shop_domain": "swifpro-demo.myshopify.com"},
        )
        if not SyncRun.objects.filter(tenant=tenant).exists():
            detail = sync_shopify_for_tenant(tenant)
            self.stdout.write(f"Ran Shopify sync: {detail}")

        # Save a draft VAT return for the current quarter (populates the VAT page).
        from core.services.vat import save_vat_return
        today = timezone.now().date()
        period_from = today.replace(day=1) - timezone.timedelta(days=62)
        save_vat_return(tenant, period_from.replace(day=1), today)
        self.stdout.write("Saved a draft VAT return.")

        # A couple of recorded expenses (posted to the GL), so the Expenses
        # page and the P&L show operating costs.
        if not Expense.objects.filter(tenant=tenant).exists():
            std_tax = TaxCode.objects.filter(tenant=tenant, code="STD").first()
            rent = GLAccount.objects.filter(tenant=tenant, code="6100").first()
            software = GLAccount.objects.filter(tenant=tenant, code="6700").first()
            if rent:
                e1 = Expense.objects.create(
                    tenant=tenant, payee="Innovation Park Estates", category=rent,
                    description="Monthly workshop rent", net_amount=Decimal("1200.00"),
                    tax_code=std_tax, paid=True, method=Expense.Method.BANK, reference="RENT-05",
                )
                post_expense(e1)
            if software:
                e2 = Expense.objects.create(
                    tenant=tenant, payee="Cloud Tools Ltd", category=software,
                    description="SaaS subscriptions", net_amount=Decimal("180.00"),
                    tax_code=std_tax, paid=True, method=Expense.Method.CARD, reference="SUB-118",
                )
                post_expense(e2)
            self.stdout.write("Seeded sample expenses (posted to GL).")

        # Sample bank statement lines (some match the seeded payment/expenses).
        if not BankTransaction.objects.filter(tenant=tenant).exists():
            today = timezone.now().date()
            td = timezone.timedelta
            BankTransaction.objects.create(tenant=tenant, txn_date=today - td(days=5),
                                           description="FPS CREDIT BRIGHT RETAIL", amount=Decimal("300.00"), reference="FPS-DEMO-1")
            BankTransaction.objects.create(tenant=tenant, txn_date=today - td(days=4),
                                           description="DD INNOVATION PARK ESTATES", amount=Decimal("-1440.00"), reference="RENT-05")
            BankTransaction.objects.create(tenant=tenant, txn_date=today - td(days=3),
                                           description="CARD CLOUD TOOLS LTD", amount=Decimal("-216.00"), reference="SUB-118")
            BankTransaction.objects.create(tenant=tenant, txn_date=today - td(days=2),
                                           description="BANK CHARGES", amount=Decimal("-12.50"))
            self.stdout.write("Seeded sample bank statement lines.")

        # A sample sales credit note against the demo AR invoice.
        if not CreditNote.objects.filter(tenant=tenant).exists():
            std_tax = TaxCode.objects.filter(tenant=tenant, code="STD").first()
            ar_inv = CustomerInvoice.objects.filter(tenant=tenant, invoice_number="INV-DEMO-0001").first()
            cn = CreditNote.objects.create(
                tenant=tenant, kind=CreditNote.Kind.SALES, credit_note_number="CN-DEMO-0001",
                customer=customer, customer_invoice=ar_inv, reason="Goodwill discount",
            )
            CreditNoteLine.objects.create(credit_note=cn, description="Goodwill discount",
                                          qty=Decimal("1"), unit_amount=Decimal("20.00"), tax_code=std_tax)
            post_credit_note(cn)
            self.stdout.write("Seeded a sample sales credit note (posted to GL).")

        # A sample per-user permission override so the Permissions editor is
        # populated: give the warehouse user finance-report access, and revoke
        # invoice management from sales.
        wh_user = User.objects.filter(username="warehouse").first()
        if wh_user:
            UserPermissionOverride.objects.get_or_create(
                tenant=tenant, user=wh_user, permission=permissions_mod.VIEW_FINANCE_REPORTS,
                defaults={"effect": UserPermissionOverride.GRANT},
            )
        sales_user = User.objects.filter(username="sales").first()
        if sales_user:
            UserPermissionOverride.objects.get_or_create(
                tenant=tenant, user=sales_user, permission=permissions_mod.MANAGE_INVOICES,
                defaults={"effect": UserPermissionOverride.REVOKE},
            )
        self.stdout.write("Seeded sample per-user permission overrides (warehouse +reports, sales -invoices).")

        # Audit log: realistic, time-spread events so the Audit Log page and its
        # CSV export show content out of the box. Seeded once.
        if not AuditLog.objects.filter(tenant=tenant, ip="203.0.113.10").exists():
            now = timezone.now()
            td = timezone.timedelta
            events = [
                (td(days=6, hours=2), "LOGIN", "owner", "", "/dashboard/"),
                (td(days=6, hours=1), "USER_INVITED", "owner", "accountant@swifpro-demo.co.uk -> accountant (ACCOUNTANT)", "/team/invite/"),
                (td(days=5, hours=5), "LOGIN", "accountant", "", "/dashboard/"),
                (td(days=5, hours=4), "ROLE_CHANGED", "owner", "manager: SALES -> MANAGER", "/users/2/role/"),
                (td(days=4, hours=8), "LOGIN_FAILED", "sales", "bad credentials", "/login/"),
                (td(days=4, hours=7), "LOGIN", "sales", "", "/dashboard/"),
                (td(days=4, hours=3), "PERMISSION_CHANGED", "owner", "warehouse: +view_finance_reports", "/users/5/permissions/"),
                (td(days=3, hours=6), "PERMISSION_CHANGED", "owner", "sales: -manage_invoices", "/users/4/permissions/"),
                (td(days=3, hours=2), "DATA_EXPORTED", "accountant", "products (3 rows)", "/export/products.csv"),
                (td(days=2, hours=9), "RECORD_DELETED", "manager", "Product SKU-OLD - Discontinued Widget", "/products/99/delete/"),
                (td(days=2, hours=1), "ACCESS_DENIED", "viewer", "You do not have permission for this action.", "/po/new/"),
                (td(days=1, hours=4), "PASSWORD_CHANGED", "accountant", "", "/account/password/"),
                (td(days=1, hours=2), "USER_DEACTIVATED", "owner", "tempcontractor", "/users/9/active/"),
                (td(hours=6), "DATA_EXPORTED", "owner", "audit log (28 rows)", "/audit/export.csv"),
                (td(hours=3), "LOGOUT", "sales", "", "/logout/"),
                (td(hours=1), "LOGIN", "owner", "", "/dashboard/"),
            ]
            for delta, action, username, detail, path in events:
                u = User.objects.filter(username=username).first()
                AuditLog.objects.create(
                    tenant=tenant, user=u, username=username, action=action,
                    detail=detail, path=path, ip="203.0.113.10",
                    created_at=now - delta,
                )
            self.stdout.write(f"Seeded {len(events)} audit log events.")

        self.stdout.write(self.style.SUCCESS("Demo data ready. Open http://127.0.0.1:8000/"))
