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
)
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
        if not tenant.onboarding_complete:
            tenant.onboarding_complete = True
            tenant.save(update_fields=["onboarding_complete"])
        self.stdout.write(("Created" if created else "Reusing") + f" tenant: {tenant.name}")

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

        self.stdout.write(self.style.SUCCESS("Demo data ready. Open http://127.0.0.1:8000/"))
