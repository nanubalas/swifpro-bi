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
    SalesQuote, SalesQuoteLine, CustomerOrder, CustomerOrderLine,
    RecurringInvoice, RecurringInvoiceLine,
)
from core.services.gl import post_expense, post_credit_note
from core.services import recurring as recurring_service
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

        # Locations (covering several location types)
        wh, _ = Location.objects.get_or_create(tenant=tenant, name="Main Warehouse", defaults={"type": Location.Type.WAREHOUSE})
        Location.objects.get_or_create(tenant=tenant, name="3PL London", defaults={"type": Location.Type.THREEPL})
        Location.objects.get_or_create(tenant=tenant, name="Returns", defaults={"type": Location.Type.RETURNS})
        Location.objects.get_or_create(tenant=tenant, name="Manchester Shop", defaults={"type": Location.Type.SHOP})
        Location.objects.get_or_create(tenant=tenant, name="Delivery Van 1", defaults={"type": Location.Type.VAN})

        # Suppliers
        globex, _ = Supplier.objects.get_or_create(tenant=tenant, name="Globex Supplies", defaults={
            "email": "sales@globex.example", "currency_code": "GBP", "contact_person": "Pat Lee",
            "phone": "+44 161 555 0100", "vat_number": "GB444555666", "company_number": "11223344",
            "address": "Globex House\nSalford\nM50 2ST", "payment_terms_days": 30,
            "bank_name": "Barclays", "bank_account_name": "Globex Supplies Ltd",
            "bank_sort_code": "20-00-00", "bank_account_number": "12345678",
            "categories": "Raw materials, Components", "notes": "Primary components supplier."})
        Supplier.objects.get_or_create(tenant=tenant, name="Acme Imports", defaults={
            "email": "orders@acme.example", "currency_code": "USD", "contact_person": "Dana Cruz",
            "phone": "+1 415 555 0142", "vat_number": "US99-1234567",
            "address": "500 Market St\nSan Francisco\nCA", "payment_terms_days": 45,
            "categories": "Imports, Logistics", "status": Supplier.Status.ACTIVE})
        Supplier.objects.get_or_create(tenant=tenant, name="Dormant Packaging Co", defaults={
            "email": "old@dormantpack.example", "currency_code": "GBP",
            "categories": "Packaging", "status": Supplier.Status.INACTIVE})

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

        # Preferred suppliers for reordering.
        Product.objects.filter(pk__in=[p1.pk, p2.pk]).update(preferred_supplier=globex)

        # Categories (with one subcategory) + product enrichment.
        from core.models import ProductCategory
        std_tax = TaxCode.objects.filter(tenant=tenant, code="STD").first()
        cat_office, _ = ProductCategory.objects.get_or_create(tenant=tenant, name="Office", parent=None)
        cat_elec, _ = ProductCategory.objects.get_or_create(tenant=tenant, name="Electronics", parent=None)
        cat_lighting, _ = ProductCategory.objects.get_or_create(tenant=tenant, name="Lighting", parent=cat_elec)
        for prod, ptype, cat, brand, sale, reorder in [
            (p1, Product.Type.FINISHED_GOOD, cat_lighting, "Aurora", "24.99", "20"),
            (p2, Product.Type.STOCK, cat_elec, "Nimbus", "14.99", "30"),
            (p3, Product.Type.STOCK, cat_office, "Halcyon", "4.99", "100"),
            (kit, Product.Type.BUNDLE, cat_office, "SwifPro", "49.99", "0"),
        ]:
            Product.objects.filter(pk=prod.pk).update(
                product_type=ptype, category=cat, brand=brand,
                sales_price=Decimal(sale), tax_code=std_tax, reorder_level=Decimal(reorder),
                description=f"{prod.name} - demo product.")

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
                      "billing_address": "10 High Street\nManchester\nM1 1AA",
                      "customer_type": Customer.Type.COMPANY, "contact_person": "Jane Bright",
                      "company_number": "09876543", "phone": "+44 161 555 0123",
                      "shipping_address": "Bright Retail Warehouse\nTrafford Park\nManchester\nM17 1AB",
                      "payment_terms_days": 30, "credit_limit": Decimal("5000.00"),
                      "tags": "VIP, Retail", "notes": "Key account - priority dispatch."},
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

        # ----------------------------------------------------------------
        # Sales module demo data: extra customers/products, quotes, sales
        # orders, invoices across every status, recurring invoices, refunds.
        # Guarded on SalesQuote so re-running the seed never duplicates.
        # ----------------------------------------------------------------
        if not SalesQuote.objects.filter(tenant=tenant).exists():
            std_tax = TaxCode.objects.filter(tenant=tenant, code="STD").first()
            red_tax = TaxCode.objects.filter(tenant=tenant, code="RED").first()
            today = timezone.now().date()
            now = timezone.now()
            td = timezone.timedelta
            am = recurring_service.add_months

            # A few more customers (varied type/status/tags/credit) so the list,
            # filters and "Sales by customer" all have variety.
            def make_customer(name, email, **extra):
                return Customer.objects.get_or_create(tenant=tenant, name=name, defaults={"email": email, **extra})[0]

            northwind = make_customer("Northwind Traders", "ap@northwind.example",
                                      customer_type=Customer.Type.TRADE, contact_person="Tom North",
                                      phone="+44 113 555 0144", payment_terms_days=30,
                                      credit_limit=Decimal("3000.00"), tags="Trade",
                                      billing_address="22 Canal Road\nLeeds\nLS1 4AB")
            meridian = make_customer("Meridian Components Ltd", "accounts@meridian.example",
                                     customer_type=Customer.Type.WHOLESALE, contact_person="Priya Shah",
                                     phone="+44 121 555 0188", vat_number="GB555111222",
                                     payment_terms_days=45, credit_limit=Decimal("10000.00"),
                                     tags="Wholesale, VIP", billing_address="Meridian House\nBirmingham\nB2 5TT")
            caldera = make_customer("Caldera Studios", "hello@caldera.example",
                                    customer_type=Customer.Type.INDIVIDUAL, contact_person="Sam Vale",
                                    phone="+44 117 555 0166", tags="Creative",
                                    billing_address="Studio 3\nBristol\nBS1 6QA")
            # An inactive / on-hold example.
            make_customer("Dormant Decor Co", "old@dormant.example",
                          customer_type=Customer.Type.COMPANY, status=Customer.Status.INACTIVE, tags="Lapsed")
            make_customer("Overdue Outfitters", "ar@overdue.example",
                          customer_type=Customer.Type.TRADE, status=Customer.Status.ON_HOLD,
                          credit_limit=Decimal("500.00"), tags="Trade", notes="On credit hold - chase payment.")

            # Two more products for richer "Sales by product".
            p4 = make_product("SKU-004", "Stratus Webcam HD", "22.00")
            p5 = make_product("SKU-005", "Lumen LED Strip", "6.50")

            def add_lines(doc, line_model, fk, items):
                for prod, qty, price, disc in items:
                    line_model.objects.create(**{
                        fk: doc, "product": prod, "description": prod.name,
                        "qty": Decimal(qty), "unit_price": Decimal(price),
                        "discount_pct": Decimal(disc), "tax_code": std_tax})

            # ---- Quotes (one per key status) ----
            q_draft = SalesQuote.objects.create(
                tenant=tenant, customer=northwind, quote_number="QUO-DEMO-0001",
                quote_date=today - td(days=3), valid_until=today + td(days=27),
                currency_code="GBP", status=SalesQuote.Status.DRAFT,
                terms=tenant.invoice_footer, notes="Initial proposal.")
            add_lines(q_draft, SalesQuoteLine, "quote", [(p1, 5, "24.99", "0"), (p4, 2, "39.99", "5")])

            q_sent = SalesQuote.objects.create(
                tenant=tenant, customer=meridian, quote_number="QUO-DEMO-0002",
                quote_date=today - td(days=6), valid_until=today + td(days=24),
                currency_code="GBP", status=SalesQuote.Status.SENT, sent_at=now - td(days=6),
                terms=tenant.invoice_footer)
            add_lines(q_sent, SalesQuoteLine, "quote", [(p2, 10, "14.99", "0")])

            q_accepted = SalesQuote.objects.create(
                tenant=tenant, customer=caldera, quote_number="QUO-DEMO-0003",
                quote_date=today - td(days=10), valid_until=today + td(days=20),
                currency_code="GBP", status=SalesQuote.Status.ACCEPTED, sent_at=now - td(days=10),
                terms=tenant.invoice_footer)
            add_lines(q_accepted, SalesQuoteLine, "quote", [(p3, 20, "4.99", "10"), (p5, 15, "9.99", "0")])

            # ---- Quote -> Sales order -> Invoice conversion chain ----
            q_converted = SalesQuote.objects.create(
                tenant=tenant, customer=northwind, quote_number="QUO-DEMO-0004",
                quote_date=today - td(days=20), currency_code="GBP",
                status=SalesQuote.Status.CONVERTED, sent_at=now - td(days=20),
                terms=tenant.invoice_footer)
            add_lines(q_converted, SalesQuoteLine, "quote", [(p1, 4, "24.99", "0"), (p2, 4, "14.99", "0")])

            so_from_quote = CustomerOrder.objects.create(
                tenant=tenant, customer=northwind, order_number="SO-DEMO-0001",
                order_date=today - td(days=18), currency_code="GBP",
                status=CustomerOrder.Status.INVOICED, quote=q_converted, terms=tenant.invoice_footer)
            add_lines(so_from_quote, CustomerOrderLine, "order", [(p1, 4, "24.99", "0"), (p2, 4, "14.99", "0")])

            # A standalone confirmed sales order (not yet invoiced).
            so_open = CustomerOrder.objects.create(
                tenant=tenant, customer=meridian, order_number="SO-DEMO-0002",
                order_date=today - td(days=2), currency_code="GBP",
                status=CustomerOrder.Status.CONFIRMED, terms=tenant.invoice_footer)
            add_lines(so_open, CustomerOrderLine, "order", [(p4, 6, "39.99", "0"), (p5, 10, "9.99", "5")])

            # ---- Invoices across every status ----
            def make_invoice(number, cust, items, inv_date, due_days=30, source_order=None, source_quote=None):
                inv = CustomerInvoice.objects.create(
                    tenant=tenant, customer=cust, invoice_number=number, invoice_date=inv_date,
                    due_date=inv_date + td(days=due_days), currency_code="GBP",
                    terms=tenant.invoice_footer, source_order=source_order, source_quote=source_quote)
                add_lines(inv, CustomerInvoiceLine, "invoice", items)
                return inv

            def receipt(inv, amount, pay_date):
                p = Payment.objects.create(
                    tenant=tenant, direction=Payment.Direction.RECEIPT, customer=inv.customer,
                    amount=Decimal(amount), method=Payment.Method.BANK, payment_date=pay_date,
                    reference=f"RCT-{inv.invoice_number}")
                PaymentAllocation.objects.create(payment=p, customer_invoice=inv, amount=Decimal(amount))
                post_payment(p)
                return p

            # Invoice generated from the converted order (issued + sent).
            inv_from_order = make_invoice("INV-DEMO-0002", northwind,
                                          [(p1, 4, "24.99", "0"), (p2, 4, "14.99", "0")],
                                          today - td(days=18), source_order=so_from_quote, source_quote=q_converted)
            post_customer_invoice(inv_from_order)
            inv_from_order.status = CustomerInvoice.Status.SENT
            inv_from_order.sent_at = now - td(days=18)
            inv_from_order.save(update_fields=["status", "sent_at"])

            # Partially paid.
            inv_partial = make_invoice("INV-DEMO-0003", meridian, [(p2, 8, "14.99", "0")], today - td(days=25))
            post_customer_invoice(inv_partial)
            receipt(inv_partial, "60.00", today - td(days=10))

            # Fully paid.
            inv_paid = make_invoice("INV-DEMO-0004", caldera, [(p3, 10, "4.99", "10"), (p5, 8, "9.99", "0")], today - td(days=40))
            post_customer_invoice(inv_paid)
            receipt(inv_paid, inv_paid.total, today - td(days=30))

            # Overdue (issued, past due, unpaid).
            inv_overdue = make_invoice("INV-DEMO-0005", northwind, [(p4, 3, "39.99", "0")], today - td(days=60), due_days=30)
            post_customer_invoice(inv_overdue)
            inv_overdue.status = CustomerInvoice.Status.SENT
            inv_overdue.sent_at = now - td(days=60)
            inv_overdue.save(update_fields=["status", "sent_at"])

            # Cancelled (draft that was voided before issue).
            inv_cancelled = make_invoice("INV-DEMO-0006", meridian, [(p1, 1, "24.99", "0")], today - td(days=5))
            inv_cancelled.status = CustomerInvoice.Status.CANCELLED
            inv_cancelled.save(update_fields=["status"])

            # Refunded (paid then refunded).
            inv_refunded = make_invoice("INV-DEMO-0007", caldera, [(p2, 2, "14.99", "0")], today - td(days=15))
            post_customer_invoice(inv_refunded)
            receipt(inv_refunded, inv_refunded.total, today - td(days=12))
            refund = Payment.objects.create(
                tenant=tenant, direction=Payment.Direction.REFUND, customer=caldera,
                amount=inv_refunded.amount_paid, method=Payment.Method.BANK,
                payment_date=today - td(days=8), reference=f"Refund {inv_refunded.invoice_number}")
            post_payment(refund)
            inv_refunded.status = CustomerInvoice.Status.REFUNDED
            inv_refunded.save(update_fields=["status"])

            # A couple more paid invoices on spread dates for the history/report charts.
            for n, (cust, items, days_ago) in enumerate([
                (northwind, [(p5, 30, "9.99", "0")], 90),
                (meridian, [(p1, 6, "24.99", "0"), (p3, 12, "4.99", "0")], 75),
                (caldera, [(p4, 4, "39.99", "0")], 50),
            ], start=8):
                inv = make_invoice(f"INV-DEMO-{n:04d}", cust, items, today - td(days=days_ago))
                post_customer_invoice(inv)
                receipt(inv, inv.total, today - td(days=days_ago - 7))

            # A draft invoice (work in progress).
            make_invoice("INV-DEMO-0011", northwind, [(p1, 2, "24.99", "0")], today)

            self.stdout.write("Seeded quotes, sales orders and invoices across every status.")

            # ---- Recurring invoice template (monthly retainer), with catch-up ----
            start = am(today, -5)
            retainer = RecurringInvoice.objects.create(
                tenant=tenant, customer=caldera, name="Monthly support retainer",
                frequency=RecurringInvoice.Frequency.MONTHLY, interval=1,
                start_date=start, next_run_date=start, auto_issue=True,
                currency_code="GBP", terms=tenant.invoice_footer,
                notes="Ongoing support and maintenance.")
            RecurringInvoiceLine.objects.create(
                template=retainer, product=None, description="Support & maintenance retainer",
                qty=Decimal("1"), unit_price=Decimal("300.00"), tax_code=std_tax)
            generated = recurring_service.generate_for_template(retainer, today=today)
            self.stdout.write(f"Seeded recurring retainer ({len(generated)} invoices generated).")

        # ----------------------------------------------------------------
        # Product master demo data: every product type, categories +
        # subcategories, variants, pack size, batch/expiry/serial tracking,
        # barcodes, sales prices and opening stock. Guarded on a marker SKU.
        # ----------------------------------------------------------------
        from core.models import ProductCategory, ProductBarcode
        std_tax = TaxCode.objects.filter(tenant=tenant, code="STD").first()
        zero_tax = TaxCode.objects.filter(tenant=tenant, code="ZERO").first()

        cat_office, _ = ProductCategory.objects.get_or_create(tenant=tenant, name="Office", parent=None)
        cat_elec, _ = ProductCategory.objects.get_or_create(tenant=tenant, name="Electronics", parent=None)
        cat_lighting, _ = ProductCategory.objects.get_or_create(tenant=tenant, name="Lighting", parent=cat_elec)
        cat_apparel, _ = ProductCategory.objects.get_or_create(tenant=tenant, name="Apparel", parent=None)
        cat_tshirts, _ = ProductCategory.objects.get_or_create(tenant=tenant, name="T-Shirts", parent=cat_apparel)
        cat_food, _ = ProductCategory.objects.get_or_create(tenant=tenant, name="Food & Drink", parent=None)
        cat_services, _ = ProductCategory.objects.get_or_create(tenant=tenant, name="Services", parent=None)
        cat_materials, _ = ProductCategory.objects.get_or_create(tenant=tenant, name="Raw Materials", parent=None)

        # Enrich the existing demo products (incl. the channel/sales ones).
        for sku, ptype, cat, brand, sale, reorder in [
            ("SKU-001", Product.Type.FINISHED_GOOD, cat_lighting, "Aurora", "24.99", "20"),
            ("SKU-002", Product.Type.STOCK, cat_elec, "Nimbus", "14.99", "30"),
            ("SKU-003", Product.Type.STOCK, cat_office, "Halcyon", "4.99", "100"),
            ("SKU-004", Product.Type.STOCK, cat_elec, "Stratus", "39.99", "15"),
            ("SKU-005", Product.Type.FINISHED_GOOD, cat_lighting, "Lumen", "9.99", "40"),
            ("SKU-KIT-001", Product.Type.BUNDLE, cat_office, "SwifPro", "49.99", "0"),
        ]:
            Product.objects.filter(tenant=tenant, sku=sku).update(
                product_type=ptype, category=cat, brand=brand, sales_price=Decimal(sale),
                tax_code=std_tax, reorder_level=Decimal(reorder))

        if not Product.objects.filter(tenant=tenant, sku="TSH").exists():
            def make_full_product(sku, name, ptype, cat, brand, sale, cost, reorder="0",
                                  parent=None, o1=None, o2=None, pack=None,
                                  lots=False, expiry=False, serial=False, tax=None, opening=0):
                p, _ = Product.objects.get_or_create(
                    tenant=tenant, sku=sku,
                    defaults={"name": name, "base_uom": each, "uom": "each"})
                Product.objects.filter(pk=p.pk).update(
                    name=name, product_type=ptype, category=cat, brand=brand,
                    sales_price=Decimal(sale), standard_cost=Decimal(cost), tax_code=(tax or std_tax),
                    reorder_level=Decimal(reorder), parent=parent, option1=o1, option2=o2,
                    pack_size=pack, track_lots=lots, track_expiry=expiry, track_serial=serial,
                    description=f"{name} - demo product.")
                p.refresh_from_db()
                if opening and not InventoryMovement.objects.filter(tenant=tenant, product=p, ref_type="OPENING").exists():
                    apply_movement(tenant=tenant, product=p, location=wh,
                                   movement_type=InventoryMovement.MovementType.RECEIVE,
                                   qty_delta=Decimal(opening), ref_type="OPENING", ref_id=sku,
                                   notes="Opening stock", unit_cost=Decimal(cost))
                return p

            # Service + non-stock + raw material.
            make_full_product("PRO-INSTALL", "On-site Installation", Product.Type.SERVICE,
                              cat_services, "SwifPro", "75.00", "0")
            make_full_product("GIFT-25", "Gift Voucher GBP25", Product.Type.NON_STOCK,
                              cat_services, "SwifPro", "25.00", "0", tax=zero_tax)
            make_full_product("RAW-ALU", "Aluminium Sheet 1m", Product.Type.RAW_MATERIAL,
                              cat_materials, "MetalCo", "0.00", "12.00", reorder="50",
                              pack="Bundle of 10", lots=True, opening="200")

            # Batch + expiry tracked, and serial tracked.
            make_full_product("FOOD-BAR", "Protein Bar 60g", Product.Type.STOCK,
                              cat_food, "FuelUp", "1.99", "0.80", reorder="200",
                              pack="Case of 24", lots=True, expiry=True, opening="480")
            make_full_product("SN-LAPTOP", "Pro Laptop 14\"", Product.Type.FINISHED_GOOD,
                              cat_elec, "Stratus", "899.00", "640.00", reorder="5",
                              serial=True, opening="8")

            # Variant parent + size/colour variants.
            tsh = make_full_product("TSH", "Classic T-Shirt", Product.Type.FINISHED_GOOD,
                                    cat_tshirts, "SwifPro", "12.99", "4.50")
            for vsku, size, colour, qty in [("TSH-S-BLK", "S", "Black", "40"),
                                            ("TSH-M-BLK", "M", "Black", "60"),
                                            ("TSH-L-BLK", "L", "Black", "35"),
                                            ("TSH-M-WHT", "M", "White", "50")]:
                make_full_product(vsku, f"Classic T-Shirt ({size} / {colour})", Product.Type.FINISHED_GOOD,
                                  cat_tshirts, "SwifPro", "12.99", "4.50", reorder="20",
                                  parent=tsh, o1=size, o2=colour, opening=qty)

            # Barcodes (EAN-13) for a few products.
            for sku, code in [("SKU-001", "5012345000018"), ("SKU-002", "5012345000025"),
                              ("FOOD-BAR", "5012345000513"), ("SN-LAPTOP", "5012345000810"),
                              ("TSH-M-BLK", "5012345001213")]:
                p = Product.objects.filter(tenant=tenant, sku=sku).first()
                if p:
                    ProductBarcode.objects.get_or_create(tenant=tenant, code=code, defaults={"product": p})

            self.stdout.write("Seeded full product master (types, categories, variants, tracking, barcodes, opening stock).")

        # Stock adjustments: approval threshold + a posted damage + a pending write-off.
        from core.models import StockAdjustment
        if tenant.stock_adjustment_approval_threshold != Decimal("100.00"):
            tenant.stock_adjustment_approval_threshold = Decimal("100.00")
            tenant.save(update_fields=["stock_adjustment_approval_threshold"])
        if not StockAdjustment.objects.filter(tenant=tenant).exists():
            admin_u = User.objects.filter(username="admin").first() or User.objects.filter(username="owner").first()
            # Small damage -> auto-posted (below threshold).
            dmg = StockAdjustment.objects.create(
                tenant=tenant, product=p2, location=wh, reason=StockAdjustment.Reason.DAMAGE,
                qty_delta=Decimal("-2.00"), notes="Crushed in transit",
                status=StockAdjustment.Status.POSTED, estimated_value=(p2.average_cost or Decimal("0")) * 2,
                requested_by=admin_u, approved_by=admin_u, posted_at=timezone.now())
            apply_movement(tenant=tenant, product=p2, location=wh,
                           movement_type=InventoryMovement.MovementType.DAMAGE, qty_delta=Decimal("-2.00"),
                           ref_type="STOCK_ADJ", ref_id=str(dmg.id), notes="Damaged stock: Crushed in transit",
                           user=admin_u)
            # Large write-off -> pending approval (above threshold).
            laptop = Product.objects.filter(tenant=tenant, sku="SN-LAPTOP").first()
            if laptop:
                StockAdjustment.objects.create(
                    tenant=tenant, product=laptop, location=wh, reason=StockAdjustment.Reason.WRITE_OFF,
                    qty_delta=Decimal("-1.00"), notes="Unit lost - investigating",
                    status=StockAdjustment.Status.PENDING,
                    estimated_value=(laptop.standard_cost or Decimal("0")), requested_by=admin_u)
            self.stdout.write("Seeded stock adjustments (1 posted damage, 1 pending write-off).")

        self.stdout.write(self.style.SUCCESS("Demo data ready. Open http://127.0.0.1:8000/"))
